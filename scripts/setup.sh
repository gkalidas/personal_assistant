#!/usr/bin/env bash
# GK Personal Assistant — one-time project setup.
#
# Installs Python dependencies and pre-downloads every model the assistant
# loads lazily, so nothing downloads in the middle of your first query.
#
# Prerequisite: Ollama must already be installed (run ./install.sh first — that
# is the stock Ollama installer) and `ollama serve` must be reachable.
#
# Safe to re-run: every step is idempotent.

set -euo pipefail

PA_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/envs/evn_personal_assistant}"
PY="$VENV_DIR/bin/python"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

# Models pulled into Ollama (router / text / fallback / diary vision).
OLLAMA_MODELS="qwen2.5:0.5b qwen3:1.7b qwen2.5:3b moondream"

info() { echo ">>> $*"; }

# ── 1. Python virtualenv + dependencies ───────────────────────────────────────
if [ ! -x "$PY" ]; then
    info "Creating virtualenv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
info "Installing Python dependencies"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r "$PA_DIR/requirements.txt"

# ── 2. Ollama models (incl. moondream, the diary vision model) ────────────────
if ! curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "WARNING: Ollama not reachable at $OLLAMA_URL — start it with 'ollama serve' and re-run." >&2
else
    for m in $OLLAMA_MODELS; do
        info "Pulling Ollama model: $m"
        ollama pull "$m"
    done
fi

# ── 3. Pre-download HuggingFace/ONNX models so they never fetch mid-query ──────
# FastEmbed router model (bge-small-en-v1.5, ~33 MB → ~/.cache/huggingface/)
info "Warming embedding router model (FastEmbed bge-small)"
cd "$PA_DIR"
"$PY" - <<'EOF' || echo "  (embedding router warm-up skipped — will download on first query)"
from core.embedding_router import warmup
# warmup() BLOCKS until the model downloads + index builds. Do NOT use
# fast_route() here: it returns immediately and this one-shot process would exit,
# killing the background build thread before the ~33 MB model finishes.
print("  embedding router ready" if warmup() else "  embedding router NOT ready (no network?)")
EOF

# InsightFace face-recognition model (buffalo_sc, ~85 MB → ~/.insightface/)
info "Warming face-recognition model (InsightFace buffalo_sc)"
"$PY" - <<'EOF' || echo "  (InsightFace warm-up skipped — will download on first diary photo run)"
from modules.faces.clusterer import _get_app
_get_app()
EOF

info "Setup complete. Start the assistant with:"
echo "    source $VENV_DIR/bin/activate && python main.py"
