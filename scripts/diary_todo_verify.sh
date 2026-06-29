#!/usr/bin/env bash
# Restart the assistant services so they load the latest code, then verify that
# (a) todo routing works and (b) diary photo-captioning is no longer timing out.
# Appends a timestamped report to scripts/verify_report.log.
#
# Created to satisfy a "restart services & verify at 12:03 and 5am" request after
# fixing todo misrouting and the moondream cold-load caption death-spiral.
set -u

PROJ="/home/ganesh/projects/personal_assistant"
VENV="/home/ganesh/envs/evn_personal_assistant"
PY="$VENV/bin/python"
LOG="$PROJ/scripts/verify_report.log"
PORT=8080

cd "$PROJ" || exit 1
exec >>"$LOG" 2>&1
echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] verify run starting"

# 1. Restart guardian (owns the idle-scheduled diary captioning).
systemctl --user restart gk-guardian.service && echo "  guardian restarted" || echo "  guardian restart FAILED"

# 2. Ensure the dashboard is running on $PORT with current code (restart it).
pkill -f "uvicorn dashboard.server:app --host 127.0.0.1 --port $PORT" 2>/dev/null && sleep 2
setsid nohup "$PY" -m uvicorn dashboard.server:app --host 127.0.0.1 --port $PORT \
    >"$PROJ/scripts/dashboard.out" 2>&1 < /dev/null &
# wait for it to answer
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/data" 2>/dev/null)
  [ "$code" = "200" ] && { echo "  dashboard up on :$PORT (${i}s)"; break; }
  sleep 1
done

# 3. Verify todo routing in-process (no HTTP dependency) + diary caption health.
"$PY" - <<'PY'
from core.embedding_router import warmup, get_router
from core.registry import build_modules
import sqlite3, datetime

ok = warmup(timeout=120)
mods = build_modules()
r = get_router()
cases = [("show todo list","todo"),("what are my tasks","todo"),
         ("add todo buy fertilizer","todo"),("spent 500 on seeds","finance"),
         ("weather today","farming"),("show diary draft","diary")]
print(f"  embed-router ready={ok}; module count={len(mods)}")
bad = 0
for q, exp in cases:
    res = r.route(q)
    got = res[0] if res else "(->LLM)"
    flag = "" if got == exp else "  <-- WRONG"
    if res and got != exp: bad += 1
    print(f"    route {got:8} (exp {exp:8}) {q!r}{flag}")
print(f"  routing: {'OK' if bad==0 else str(bad)+' WRONG'}")

# diary caption health: recent vision_caption_failed entries
con = sqlite3.connect("personal_assistant.db"); con.row_factory = sqlite3.Row
since = (datetime.datetime.now() - datetime.timedelta(hours=6)).isoformat()
n = con.execute("SELECT COUNT(*) FROM mistake_log WHERE error_type='vision_caption_failed' AND ts>?", (since,)).fetchone()[0]
last = con.execute("SELECT ts,substr(details,1,80) d FROM mistake_log WHERE error_type='vision_caption_failed' ORDER BY ts DESC LIMIT 1").fetchone()
print(f"  diary vision_caption_failed in last 6h: {n}")
print(f"  most recent caption failure: {dict(last) if last else 'none ever'}")
try:
    from modules.diary.module import get_photo_stats
    s = get_photo_stats(); print(f"  photos processed total: {s.get('total_processed')}")
except Exception as e:
    print(f"  photo-stats unavailable: {e}")
PY

echo "[$(date '+%Y-%m-%d %H:%M:%S')] verify run done"
