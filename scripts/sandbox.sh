#!/bin/sh
# Launch the board against the fictional sandbox workspace — a second, parallel
# universe for testing. Nothing outside sandbox/ is read or written.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export GC_ROOT="$REPO/sandbox"
export GC_DATA="$REPO/sandbox/.data"
mkdir -p "$GC_DATA/journal" "$REPO/sandbox/inbox/gc-threads"
exec python3 "$REPO/superboard/server.py" --port "${SANDBOX_PORT:-47899}"
