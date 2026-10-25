#!/usr/bin/env sh
# Ring the user via AI Phone and print the call result as JSON.
# Usage: ring.sh "reason for the call"
set -eu

# Settings come from the environment, or from ~/.config/ai-phone/ring-me.env
# (some agents, like OpenClaw, block secret-looking env vars from reaching skills).
CONFIG="${AI_PHONE_CONFIG:-$HOME/.config/ai-phone/ring-me.env}"
if [ -z "${AI_PHONE_TOKEN:-}" ] && [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG"
fi
AI_PHONE_URL="${AI_PHONE_URL:-http://localhost:8080}"
: "${AI_PHONE_TOKEN:?no token: put AI_PHONE_TOKEN=... (the app's API_TOKEN) in $CONFIG}"
REASON="${1:?usage: ring.sh \"reason for the call\"}"

BODY=$(python3 -c 'import json,sys; print(json.dumps({"reason": sys.argv[1]}))' "$REASON")

RING=$(curl -sS --fail-with-body -X POST "$AI_PHONE_URL/api/ring" \
  -H "Authorization: Bearer $AI_PHONE_TOKEN" -H "Content-Type: application/json" -d "$BODY")

ID=$(printf '%s' "$RING" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
LISTENERS=$(printf '%s' "$RING" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("listeners", 0))')
if [ "$LISTENERS" = "0" ]; then
  echo "warning: no browser has the AI Phone page open right now; the call will likely be missed" >&2
fi

curl -sS --fail-with-body --max-time 660 \
  -H "Authorization: Bearer $AI_PHONE_TOKEN" \
  "$AI_PHONE_URL/api/ring/$ID?wait=1&timeout=600"
echo
