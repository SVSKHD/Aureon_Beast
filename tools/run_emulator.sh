#!/usr/bin/env bash
# Start the Firestore emulator without Docker, via firebase-tools (needs Node + Java).
#
# Usage:  tools/run_emulator.sh [port]
#
# Exports nothing: the caller sets FIRESTORE_EMULATOR_HOST. See the Makefile.
set -euo pipefail

PORT="${1:-8080}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${AUREON_EMULATOR_DIR:-/tmp/aureon-emulator}"

mkdir -p "$WORK"
sed "s/8080/$PORT/" "$HERE/firebase.json" > "$WORK/firebase.json"
cp "$HERE/firestore.rules" "$WORK/firestore.rules"

cd "$WORK"
echo "starting Firestore emulator on 127.0.0.1:$PORT (logs: $WORK/emulator.log)"
npx --yes firebase-tools emulators:start \
    --only firestore --project aureon-test > "$WORK/emulator.log" 2>&1 &
echo $! > "$WORK/emulator.pid"

for _ in $(seq 1 60); do
  if curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:$PORT/"; then
    echo "emulator ready:  export FIRESTORE_EMULATOR_HOST=127.0.0.1:$PORT"
    exit 0
  fi
  sleep 1
done
echo "emulator failed to start; see $WORK/emulator.log" >&2
exit 1
