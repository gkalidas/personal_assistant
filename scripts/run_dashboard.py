#!/usr/bin/env python
"""Launch the GK system dashboard and open it in the browser."""

import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from core.log import setup_logging
setup_logging()

import uvicorn
from dashboard.server import app

PORT  = int(os.getenv("DASHBOARD_PORT", "8765"))
HOST  = os.getenv("DASHBOARD_HOST", "0.0.0.0")  # nosec B104 — intentional for Tailscale mobile access
_ROOT = Path(__file__).parent.parent
_ACCESS_FILE = _ROOT / "docs" / "remote_access.md"


def _tailscale_ip() -> str:
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"],
                                      stderr=subprocess.DEVNULL, timeout=3)
        return out.decode().strip()
    except Exception:
        return ""


def _update_access_file(ts_ip: str) -> None:
    """Overwrite docs/remote_access.md with the current URLs."""
    remote = f"http://{ts_ip}:{PORT}" if ts_ip else "*(Tailscale not connected — run: sudo tailscale up)*"
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    _ACCESS_FILE.write_text(
        f"# Remote Access\n\n"
        f"| | URL |\n"
        f"|---|---|\n"
        f"| Local | http://localhost:{PORT} |\n"
        f"| Phone / Remote | {remote} |\n\n"
        f"> Tailscale IP is stable — bookmark the Phone URL once, it never changes.\n"
        f"> Last updated: {updated}\n",
        encoding="utf-8",
    )


def _open_browser():
    time.sleep(1.8)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    ts_ip = _tailscale_ip()
    _update_access_file(ts_ip)

    print(f"\n  GK Dashboard")
    print(f"  ┌─────────────────────────────────────────────┐")
    print(f"  │  Local    http://localhost:{PORT}             │")
    if ts_ip:
        print(f"  │  Phone    http://{ts_ip}:{PORT}        │")
        print(f"  │           (bookmark this — never changes)   │")
    else:
        print(f"  │  Phone    Tailscale not connected            │")
        print(f"  │           run: sudo tailscale up             │")
    print(f"  └─────────────────────────────────────────────┘")
    print(f"  Ref → docs/remote_access.md  (commit to see on GitHub)")
    print(f"  Ctrl-C to stop\n")

    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
