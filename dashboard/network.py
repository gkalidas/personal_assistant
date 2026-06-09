"""
Network monitor — detects interfaces, checks internet connectivity, measures
throughput, and handles failover alerting.

Priority order: LAN > WiFi > Tethering (BT/USB) > Unknown
Connectivity is verified by a TCP connect to 8.8.8.8:53 (no data sent).
Throughput is measured from io_counter deltas — no external requests needed.
"""

import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import psutil

log = logging.getLogger(__name__)

_PRIORITY = ["lan", "wifi", "tethering", "vpn", "unknown"]

_LAN_RE  = re.compile(r"^(eth|enp|eno|ens|em|enx)\d")
_WIFI_RE = re.compile(r"^(wlan|wlp|wlo|wls)\d")
_BT_RE   = re.compile(r"^(bnep|pan|bt)\d")
_USB_RE  = re.compile(r"^(usb|rndis|ncm)\d")
_VPN_RE  = re.compile(r"^(tun|tap|wg|tailscale|vpn)\d*")


def _iface_type(name: str) -> str:
    n = name.lower()
    if _LAN_RE.match(n):                     return "lan"
    if _WIFI_RE.match(n):                    return "wifi"
    if _BT_RE.match(n) or _USB_RE.match(n): return "tethering"
    if _VPN_RE.match(n):                     return "vpn"
    return "unknown"


def _check_connectivity(bind_ip: Optional[str] = None, timeout: float = 3.0) -> bool:
    """TCP connect to 8.8.8.8:53 — fast, no DNS, no data sent."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        if bind_ip:
            s.bind((bind_ip, 0))
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except Exception:
        return False


def _measure_speed(ifaces: list[str], duration: float = 5.0) -> tuple[float, float]:
    """Return (download_mbps, upload_mbps) measured over `duration` seconds."""
    try:
        c1 = psutil.net_io_counters(pernic=True)
        time.sleep(duration)
        c2 = psutil.net_io_counters(pernic=True)
        rx = tx = 0
        for name in ifaces:
            if name in c1 and name in c2:
                rx += c2[name].bytes_recv - c1[name].bytes_recv
                tx += c2[name].bytes_sent - c1[name].bytes_sent
        return rx * 8 / duration / 1e6, tx * 8 / duration / 1e6
    except Exception as e:
        log.debug("speed measure error: %s", e)
        time.sleep(duration)
        return 0.0, 0.0


@dataclass
class IfaceInfo:
    name: str
    type: str                       # lan / wifi / tethering / unknown
    is_up: bool
    ip: str
    link_mbps: Optional[int]        # hardware link speed (None if unknown)
    is_connected: bool = False      # has actual internet


@dataclass
class NetworkStatus:
    interfaces: list[IfaceInfo] = field(default_factory=list)
    primary: str = ""               # name of the active connected interface
    download_mbps: float = 0.0
    upload_mbps: float = 0.0
    failover_alert: bool = False    # True when primary just changed or is gone


class NetworkMonitor:
    """
    Daemon thread that:
      - Scans interfaces and checks connectivity every CHECK_INTERVAL seconds
      - Measures throughput continuously (SPEED_INTERVAL sample window)
      - Triggers failover_alert when the primary interface goes down
    """

    CHECK_INTERVAL = 60   # connectivity re-check
    SPEED_INTERVAL = 2    # throughput sample window — matches WS push rate

    def __init__(self):
        self._status = NetworkStatus()
        self._lock = threading.Lock()
        self._primary: Optional[str] = None
        self._last_check: float = 0.0   # 0 → check immediately on first loop
        t = threading.Thread(target=self._run, daemon=True, name="net-monitor")
        t.start()
        log.info("NetworkMonitor started")

    @property
    def status(self) -> NetworkStatus:
        with self._lock:
            return self._status

    # ── Background loop ───────────────────────────────────────────────────────

    def _run(self):
        while True:
            now = time.monotonic()
            if now - self._last_check >= self.CHECK_INTERVAL:
                self._do_connectivity_check()
                self._last_check = time.monotonic()
            self._do_speed_sample()   # sleeps SPEED_INTERVAL internally

    def _scan_interfaces(self) -> list[IfaceInfo]:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        result = []
        for name, stat in stats.items():
            if name == "lo":
                continue
            ip = next(
                (a.address for a in addrs.get(name, [])
                 if a.family == socket.AF_INET),
                "",
            )
            result.append(IfaceInfo(
                name=name,
                type=_iface_type(name),
                is_up=stat.isup,
                ip=ip,
                link_mbps=stat.speed if stat.speed > 0 else None,
            ))
        result.sort(key=lambda i: _PRIORITY.index(i.type) if i.type in _PRIORITY else 99)
        return result

    def _do_connectivity_check(self):
        ifaces = self._scan_interfaces()

        for iface in ifaces:
            if iface.is_up and iface.ip:
                iface.is_connected = _check_connectivity(iface.ip)
            else:
                iface.is_connected = False

        # Best connected interface (highest priority)
        new_primary: Optional[str] = None
        for iface in ifaces:
            if iface.is_connected:
                if new_primary is None:
                    new_primary = iface.name
                    break   # already sorted by priority

        # Failover detection
        prev = self._primary
        failover = False
        if prev is not None and prev != new_primary:
            if new_primary:
                log.warning("FAILOVER: %s → %s", prev, new_primary)
            else:
                log.warning("FAILOVER: %s → no connectivity", prev)
            failover = True

        self._primary = new_primary

        with self._lock:
            self._status.interfaces = ifaces
            self._status.primary = new_primary or ""
            self._status.failover_alert = failover or (new_primary is None and bool(ifaces))

        log.info(
            "net check: primary=%s connected=%s",
            new_primary,
            [i.name for i in ifaces if i.is_connected],
        )

    def _do_speed_sample(self):
        with self._lock:
            # Exclude VPN tunnels — they re-encapsulate traffic that already
            # appears on the underlying physical interface, causing double-counting.
            active = [
                i.name for i in self._status.interfaces
                if i.is_up and i.type != "vpn"
            ]
        dl, ul = _measure_speed(active, duration=self.SPEED_INTERVAL)
        with self._lock:
            self._status.download_mbps = dl
            self._status.upload_mbps = ul
