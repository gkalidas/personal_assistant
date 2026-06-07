#!/usr/bin/env bash
# Regenerate weather chart and push to GitHub.
# Runs every 6 hours via cron.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/envs/evn_personal_assistant/bin/activate"
LOG="$REPO_DIR/dashboard/update.log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

source "$VENV"
cd "$REPO_DIR"

python scripts/weather_chart.py >> "$LOG" 2>&1

git add dashboard/weather.png
if git diff --cached --quiet; then
    echo "No change in chart, skipping commit." >> "$LOG"
else
    git commit -m "chore: update weather dashboard $(date '+%Y-%m-%d %H:%M')" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1
    echo "Pushed." >> "$LOG"
fi
