#!/usr/bin/env bash
# Night download queue — runs at 23:00 via cron.
# Uses lowest CPU + IO priority so it never slows down your daytime work.
# Each item is idempotent — already-done items are skipped automatically.

LOG="/home/ganesh/projects/personal_assistant/logs/night_downloads.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "===== Night download queue started ====="

# Run everything at idle priority
NICE="nice -n 19"
IONICE="ionice -c 3"

# ── 1. Ollama models ──────────────────────────────────────────────────────────
pull_model() {
    local model="$1"
    if ollama list 2>/dev/null | grep -q "^${model}"; then
        log "SKIP  ollama:${model} (already downloaded)"
    else
        log "START ollama:${model}"
        $NICE $IONICE ollama pull "$model" >> "$LOG" 2>&1
        if ollama list 2>/dev/null | grep -q "^${model}"; then
            log "DONE  ollama:${model}"
        else
            log "FAIL  ollama:${model} — will retry next night"
        fi
    fi
}

# Small model first — ~394MB, completes in one night even on slow internet.
# Large model only starts after small one is confirmed done.
pull_model "qwen2.5:0.5b"

if ollama list 2>/dev/null | grep -q "^qwen2.5:0.5b"; then
    pull_model "qwen3:1.7b"
else
    log "SKIP  qwen3:1.7b — waiting for qwen2.5:0.5b to finish first"
fi

# ── 2. Docker ─────────────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    log "SKIP  docker (already installed: $(docker --version 2>/dev/null))"
else
    log "START docker install"
    $NICE $IONICE sudo apt-get install -y docker.io >> "$LOG" 2>&1 \
        && sudo systemctl enable --now docker >> "$LOG" 2>&1 \
        && sudo usermod -aG docker ganesh >> "$LOG" 2>&1 \
        && log "DONE  docker" \
        || log "FAIL  docker install"
fi

# ── Add new items above this line ─────────────────────────────────────────────

log "===== Night download queue finished ====="
