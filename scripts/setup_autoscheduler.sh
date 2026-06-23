#!/usr/bin/env bash
# (Re)arm the OS-level autonomous-continuation timers.
#
# Creates two one-shot systemd --user timers that run scripts/autonomous_continue.sh
# at +Nh from now. Unlike the in-session crons, these survive logout/reboot
# (requires `loginctl enable-linger $USER`, already enabled on this host) and
# fire even if the editor/Claude process has exited.
#
# Usage:   scripts/setup_autoscheduler.sh [hours1] [hours2 ...]
#          scripts/setup_autoscheduler.sh           # defaults to 4h and 8h
#
# Note: the job needs the Claude Code CLI on PATH to do real work
#       (npm install -g @anthropic-ai/claude-code). Without it, it logs a
#       "claude-cli-missing" notice and exits cleanly.

set -euo pipefail

SCRIPT="/home/ganesh/projects/personal_assistant/scripts/autonomous_continue.sh"
HOURS=("$@")
[ ${#HOURS[@]} -eq 0 ] && HOURS=(4 8)

for h in "${HOURS[@]}"; do
  unit="gk-continue-${h}h"
  systemctl --user stop "${unit}.timer" 2>/dev/null || true
  systemctl --user reset-failed 2>/dev/null || true
  systemd-run --user --on-active="${h}h" --unit="$unit" \
    --description="GK autonomous continuation +${h}h" "$SCRIPT"
done

echo
echo "Scheduled timers:"
systemctl --user list-timers 'gk-continue-*' --all
