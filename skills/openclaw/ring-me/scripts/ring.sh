#!/usr/bin/env sh
# Ring the user via AI Phone and print the call result as JSON.
# Usage: ring.sh "reason for the call"
set -eu

: "${AI_PHONE_URL:?set AI_PHONE_URL, e.g. http://localhost:8080}"
: "${AI_PHONE_TOKEN:?set AI_PHONE_TOKEN to the app's API_TOKEN}"
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
