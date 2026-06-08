#!/usr/bin/env python
"""Launch the GK system dashboard and open it in the browser."""

import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from core.log import setup_logging
setup_logging()

import uvicorn
from dashboard.server import app

PORT = int(os.getenv("DASHBOARD_PORT", "8765"))
HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")


def _open_browser():
    time.sleep(1.8)
    url = f"http://localhost:{PORT}"
    print(f"  Opening {url}")
    webbrowser.open(url)


if __name__ == "__main__":
    print(f"\n  GK Dashboard → http://localhost:{PORT}")
    print("  Ctrl-C to stop\n")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
