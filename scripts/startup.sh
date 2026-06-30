#!/usr/bin/env bash
# GK startup script — runs at boot, waits 2 min, then starts services one-by-one
# only when the system is idle. Never forces heavy tasks while you're using the machine.
#
# Services started (in order):
#   1. Ollama           — model server (required by everything)
#   2. Farming server   — http://localhost:5002  (disease diagnosis, crop data)
#   3. Model warmup     — loads qwen2.5:3b into RAM so first query is fast
#   4. Dashboard        — http://localhost:8080  (web UI)
#   5. Tailscale serve  — HTTPS proxy → localhost:8080 for phone/voice access
#
# The dashboard binds to 127.0.0.1 only (never 0.0.0.0 — that would expose it on
# LAN/WiFi too). Remote/phone access goes through `tailscale serve`, which fronts
# localhost:8080 with a real HTTPS cert on the tailnet name. HTTPS is required so
# iOS Safari grants microphone access (getUserMedia needs a secure context), which
# is what makes "Voice from mobile" work. Phone URL: https://<node>.<tailnet>.ts.net

set -euo pipefail

PA_DIR="/home/ganesh/projects/personal_assistant"
FARMING_DIR="/home/ganesh/projects/farming"
VENV_PA="/home/ganesh/envs/evn_personal_assistant/bin/python"
OLLAMA_URL="http://localhost:11434"
LOG="$PA_DIR/logs/startup.log"
mkdir -p "$PA_DIR/logs"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# ── 1. Initial delay — give the user time to log in and settle ─────────────────
log "GK startup: waiting 120 seconds before first service..."
sleep 120

# ── Helper: wait until idle before doing anything heavy ───────────────────────
wait_for_idle() {
    local task="$1"
    local max_wait="${2:-600}"   # give up and run anyway after 10 min
    local elapsed=0
    while true; do
        idle=$("$VENV_PA" - <<'EOF'
import sys
sys.path.insert(0, '/home/ganesh/projects/personal_assistant')
from security.load_monitor import is_idle
print("yes" if is_idle() else "no")
EOF
        )
        if [ "$idle" = "yes" ]; then
            log "System idle — starting: $task"
            return 0
        fi
        if [ "$elapsed" -ge "$max_wait" ]; then
            log "Max wait reached ($max_wait s) — starting anyway: $task"
            return 0
        fi
        sleep 30
        elapsed=$((elapsed + 30))
    done
}

# ── 2. Ollama ──────────────────────────────────────────────────────────────────
if ! pgrep -x ollama &>/dev/null; then
    wait_for_idle "Ollama"
    log "Starting Ollama..."
    nohup ollama serve >> "$PA_DIR/logs/ollama.log" 2>&1 &
    sleep 10
    log "Ollama started (PID $!)"
else
    log "Ollama already running — skipping"
fi

# ── 3. Farming server ──────────────────────────────────────────────────────────
if ! curl -sf http://localhost:5002/api/status &>/dev/null; then
    wait_for_idle "Farming server"
    log "Starting farming server..."
    nohup "$FARMING_DIR/run.sh" >> "$PA_DIR/logs/farming.log" 2>&1 &
    sleep 8
    log "Farming server started"
else
    log "Farming server already running — skipping"
fi

# ── 4. Model warmup — load qwen2.5:3b into RAM quietly ────────────────────────
wait_for_idle "Model warmup"
log "Warming up qwen2.5:3b (keep_alive=30m)..."
curl -s -X POST "$OLLAMA_URL/api/chat" \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen2.5:3b","messages":[{"role":"user","content":"hi"}],"stream":false,"keep_alive":"30m"}' \
    >> "$PA_DIR/logs/startup.log" 2>&1 &
log "Model warmup queued in background"

# ── 5. Dashboard server ────────────────────────────────────────────────────────
if ! curl -sf http://localhost:8080/ &>/dev/null; then
    wait_for_idle "Dashboard"
    log "Starting GK dashboard on :8080..."
    cd "$PA_DIR"
    nohup "$VENV_PA" -m uvicorn dashboard.server:app \
        --host 127.0.0.1 --port 8080 --workers 1 \
        >> "$PA_DIR/logs/dashboard.log" 2>&1 &
    sleep 5
    log "Dashboard started — http://localhost:8080"
else
    log "Dashboard already running — skipping"
fi

# ── 6. Tailscale serve — HTTPS proxy for phone / voice-from-mobile ─────────────
# Fronts localhost:8080 with a tailnet HTTPS cert so iOS Safari allows the mic.
# Idempotent: re-running just refreshes the existing serve config.
if command -v tailscale &>/dev/null; then
    if tailscale serve --bg 8080 >> "$PA_DIR/logs/startup.log" 2>&1; then
        ts_host=$(tailscale status --json 2>/dev/null \
            | "$VENV_PA" -c 'import sys,json;d=json.load(sys.stdin);s=d.get("Self",{});print((s.get("DNSName") or "").rstrip("."))' 2>/dev/null)
        log "Tailscale serve up — phone URL: https://${ts_host:-<node>.<tailnet>.ts.net}"
    else
        log "Tailscale serve failed — run scripts/enable_phone_voice.sh once (needs sudo: sets operator + HTTPS cert). Phone voice unavailable until then."
    fi
else
    log "tailscale not installed — skipping phone HTTPS proxy"
fi

log "GK startup complete."
