#!/usr/bin/env bash
# One-time setup for "Voice from mobile" (todo #10).
#
# Why this is separate from startup.sh: it needs root *once* to grant your user
# control of Tailscale and to provision the tailnet HTTPS certificate. After this
# runs successfully, startup.sh's `tailscale serve --bg 8080` works as your user
# with no sudo, and the phone can reach the dashboard over HTTPS.
#
# HTTPS matters because iOS Safari only grants microphone access (getUserMedia,
# used by voice.js) in a secure context — plain http://<tailscale-ip>:8080 will
# NOT prompt for the mic. `tailscale serve` fronts localhost:8080 with a real
# cert on your tailnet name, which satisfies that.
#
# Prerequisite you must do in the browser first (one-time, can't be scripted):
#   Tailscale admin console → DNS → enable "HTTPS Certificates".
#   https://login.tailscale.com/admin/dns
#
# Usage:  ./scripts/enable_phone_voice.sh
set -euo pipefail

DASH_PORT=8080
USER_NAME="$(id -un)"

echo "==> Granting $USER_NAME operator control of Tailscale (one-time, needs sudo)…"
sudo tailscale set --operator="$USER_NAME"

NODE="$(tailscale status --json | python3 -c 'import sys,json;print((json.load(sys.stdin).get("Self",{}).get("DNSName") or "").rstrip("."))')"
if [ -z "$NODE" ]; then
    echo "!! Could not resolve this node's tailnet name. Is Tailscale up? (tailscale status)"; exit 1
fi
echo "==> Node tailnet name: $NODE"

echo "==> Provisioning / verifying HTTPS certificate…"
if ! tailscale cert "$NODE" >/dev/null 2>&1; then
    echo "!! Cert provisioning failed."
    echo "   Enable 'HTTPS Certificates' in the admin console, then re-run this script:"
    echo "   https://login.tailscale.com/admin/dns"
    exit 1
fi
echo "   Certificate OK."

echo "==> Confirming the dashboard is listening on localhost:$DASH_PORT…"
if ! curl -sf "http://localhost:$DASH_PORT/" >/dev/null; then
    echo "!! Dashboard not running on :$DASH_PORT — start it (scripts/startup.sh) first."; exit 1
fi

echo "==> Starting tailscale serve (HTTPS → localhost:$DASH_PORT)…"
tailscale serve --bg "$DASH_PORT"
tailscale serve status

cat <<EOF

✅ Done. On your phone (with Tailscale connected to this tailnet), open:

      https://$NODE

   Then use the mic / voice button as usual. iOS will now prompt for mic access
   because the connection is HTTPS.
EOF
