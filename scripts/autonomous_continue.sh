#!/usr/bin/env bash
# Headless autonomous continuation of the GK Personal Assistant build.
# Invoked by a systemd --user timer (see scripts/setup_autoscheduler.sh).
#
# Finds the Claude Code CLI, then runs it in print/non-interactive mode against
# scripts/continuation_prompt.txt so it resumes the remaining work on its own.
# Logs everything; fails loudly (but harmlessly) if the CLI is not installed.

set -uo pipefail

# systemd --user services start with a minimal PATH — make sure node (/usr/bin)
# and the npm-global claude (/usr/local/bin) are both reachable.
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

REPO="/home/ganesh/projects/personal_assistant"
PROMPT="$REPO/scripts/continuation_prompt.txt"
LOGDIR="$REPO/logs/autonomous"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOGDIR/run_$STAMP.log"

mkdir -p "$LOGDIR"
exec >>"$LOG" 2>&1
echo "=== autonomous continue @ $STAMP ==="

# Locate the claude CLI across the usual install locations.
find_claude() {
  for c in claude \
           "$HOME/.local/bin/claude" \
           "$HOME/.npm-global/bin/claude" \
           "/usr/local/bin/claude" \
           "$HOME/.claude/local/claude"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then
      command -v "$c" 2>/dev/null || echo "$c"
      return 0
    fi
  done
  return 1
}

CLAUDE="$(find_claude)" || {
  echo "ERROR: claude CLI not found. Install it first, e.g.:"
  echo "  npm install -g @anthropic-ai/claude-code"
  echo "Then this scheduled job will work unchanged."
  echo "claude-cli-missing" > "$LOGDIR/STATUS.txt"
  exit 0
}
echo "using claude: $CLAUDE"

cd "$REPO" || exit 1
# Print mode, autonomous tool use. --dangerously-skip-permissions lets it edit
# and commit without interactive prompts (intended for this trusted local repo).
"$CLAUDE" -p "$(cat "$PROMPT")" \
  --dangerously-skip-permissions \
  --model claude-opus-4-8 \
  >>"$LOG" 2>&1

echo "=== finished @ $(date +%H:%M:%S), exit=$? ==="
