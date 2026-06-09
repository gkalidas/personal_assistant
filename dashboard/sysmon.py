"""System resource snapshot — CPU, RAM, disk, uptime."""

import os
import time
import psutil


_GiB = 1024 ** 3

# Prime the psutil CPU sampler once at import so subsequent calls with
# interval=None return immediately (uses delta from the last sample).
psutil.cpu_percent(interval=None)


def get_system_stats() -> dict:
    cpu_pct   = psutil.cpu_percent(interval=None)   # non-blocking — uses last sample
    ram       = psutil.virtual_memory()
    swap      = psutil.swap_memory()
    disk      = psutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    freq      = psutil.cpu_freq()

    uptime_s  = int(time.time() - psutil.boot_time())
    h, rem    = divmod(uptime_s, 3600)
    m         = rem // 60

    # RAM percent: psutil uses (total-available)/total — matches system monitors
    # GB values: use 1024³ (GiB) so numbers match GNOME monitor / htop
    return {
        "cpu_pct":       round(cpu_pct, 1),
        "cpu_cores":     psutil.cpu_count(logical=True),
        "cpu_physical":  psutil.cpu_count(logical=False),
        "cpu_freq_mhz":  round(freq.current) if freq else 0,
        "ram_pct":       round(ram.percent, 1),
        "ram_used_gb":   round(ram.used      / _GiB, 1),
        "ram_total_gb":  round(ram.total     / _GiB, 1),
        "swap_pct":      round(swap.percent, 1),
        "swap_used_gb":  round(swap.used     / _GiB, 1),
        "swap_total_gb": round(swap.total    / _GiB, 1),
        "disk_pct":      round(disk.percent, 1),
        "disk_used_gb":  round(disk.used     / _GiB, 1),
        "disk_total_gb": round(disk.total    / _GiB, 1),
        "uptime":        f"{h}h {m}m",
        "load_avg":      f"{load1:.2f}  {load5:.2f}  {load15:.2f}",
    }
