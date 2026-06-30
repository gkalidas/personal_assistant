#!/usr/bin/env bash
# Manage the local SearXNG instance used by the search module (todo #7).
#
# Runs from source (no Docker) because Docker isn't installed here and installing
# it needs root. SearXNG needs Python 3.11+ (tomllib), so it lives in its own
# 3.12 venv, separate from the assistant's 3.10 venv.
#
#   Repo:     ~/searxng              (shallow clone of searxng/searxng)
#   Venv:     ~/envs/searxng         (python3.12)
#   Settings: ~/searxng/my_settings.yml   (binds 127.0.0.1:8888, json format on)
#
# Usage:  scripts/searxng.sh {start|stop|restart|status}
set -euo pipefail

REPO="$HOME/searxng"
VENV="$HOME/envs/searxng/bin/python"
SETTINGS="$REPO/my_settings.yml"
LOG="$REPO/logs/searxng.out"
URL="http://localhost:8888"

is_up() { curl -sf -m 4 "$URL/" >/dev/null 2>&1; }

start() {
    if is_up; then echo "SearXNG already up at $URL"; return 0; fi
    mkdir -p "$REPO/logs"
    echo "Starting SearXNG…"
    # searx is run from source (not pip-installed), so PYTHONPATH must point at
    # the repo for `-m searx.webapp` to resolve regardless of caller cwd.
    SEARXNG_SETTINGS_PATH="$SETTINGS" PYTHONPATH="$REPO" \
        setsid nohup "$VENV" -m searx.webapp >> "$LOG" 2>&1 < /dev/null &
    for _ in $(seq 1 30); do is_up && { echo "SearXNG up at $URL"; return 0; }; sleep 1; done
    echo "SearXNG failed to come up — see $LOG"; return 1
}

stop() {
    pkill -f "searx.webapp" 2>/dev/null && echo "SearXNG stopped" || echo "SearXNG not running"
}

status() {
    if is_up; then
        echo "SearXNG: UP ($URL)"
    else
        echo "SearXNG: DOWN"
    fi
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *) echo "usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac
