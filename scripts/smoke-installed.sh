#!/usr/bin/env bash
# Automated macOS smoke test for an already-installed Superboard wheel.
#
# GitHub Actions and local verification both call this file. Keep the product
# assertions here, not copied into CI YAML, so the two paths cannot drift.
set -euo pipefail

SB_SMOKE_PYTHON="${SB_SMOKE_PYTHON:-$(command -v python3)}"

# Freshness gate. This script only ever tests what is ALREADY installed, so a
# wheel left in dist/ from an older commit can pass it silently. The version
# string cannot catch that: Superboard's number travels with the code it is
# projected from, so many commits legitimately carry the same one. The commit
# is the only honest stamp, so the build writes it and this checks it. Absent
# stamp = someone is testing an install we did not build; that stays allowed.
SB_SMOKE_REPO="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$SB_SMOKE_REPO/dist/BUILD_SHA" ]; then
  built_from="$(cat "$SB_SMOKE_REPO/dist/BUILD_SHA")"
  head_now="$(git -C "$SB_SMOKE_REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ "$built_from" != "$head_now" ]; then
    echo "smoke: installed wheel was built from $built_from, checkout is at $head_now — stale" >&2
    exit 1
  fi
  echo "smoke: installed wheel was built from $head_now"
fi
SB_SMOKE_ROOT="${SB_SMOKE_ROOT:-$(mktemp -d "${TMPDIR:-/tmp}/superboard-smoke.XXXXXX")}"
SB_SMOKE_LABEL="${SB_SMOKE_LABEL:-local}"
SB_SMOKE_PORT="${SB_SMOKE_PORT:-47850}"
SB_SMOKE_WORKSPACE="$SB_SMOKE_ROOT/workspace-$SB_SMOKE_LABEL"
SB_SMOKE_COLLISION="$SB_SMOKE_ROOT/collision-$SB_SMOKE_LABEL"
SB_SMOKE_LOG="$SB_SMOKE_ROOT/superboard-$SB_SMOKE_LABEL.log"
SB_SMOKE_COLLISION_LOG="$SB_SMOKE_ROOT/superboard-collision-$SB_SMOKE_LABEL.log"
SB_SMOKE_HTML="$SB_SMOKE_ROOT/superboard-$SB_SMOKE_LABEL.html"
SB_SMOKE_CAPTURE="$SB_SMOKE_ROOT/quick-capture-$SB_SMOKE_LABEL.json"
SB_SMOKE_CLEAN_PATH="$(dirname "$SB_SMOKE_PYTHON"):/usr/bin:/bin"

mkdir -p "$SB_SMOKE_WORKSPACE" "$SB_SMOKE_COLLISION"
if PATH="$SB_SMOKE_CLEAN_PATH" command -v claude >/dev/null 2>&1; then
  echo "smoke: invalid no-Claude test — claude is unexpectedly available" >&2
  exit 1
fi

# Run from the fresh workspace, not the repository. Python puts the current working
# directory first on sys.path; launching here from the checkout would silently import
# `superboard/` source instead of the wheel this smoke claims to verify.
SB_SMOKE_IMPORTED_FROM="$({
  cd "$SB_SMOKE_WORKSPACE"
  "$SB_SMOKE_PYTHON" -c 'import pathlib, superboard; print(pathlib.Path(superboard.__file__).resolve())'
})"
case "$SB_SMOKE_IMPORTED_FROM" in
  "$SB_SMOKE_REPO"/*)
    echo "smoke: imported checkout source instead of installed wheel: $SB_SMOKE_IMPORTED_FROM" >&2
    exit 1
    ;;
esac
echo "smoke: installed module $SB_SMOKE_IMPORTED_FROM"

expected_version="$("$SB_SMOKE_PYTHON" -c 'import re, sys; print(re.search(r"(?m)^version = \"([^\"]+)\"$", open(sys.argv[1], encoding="utf-8").read()).group(1))' "$SB_SMOKE_REPO/pyproject.toml")"
reported_version="$(PATH="$SB_SMOKE_CLEAN_PATH" "$SB_SMOKE_PYTHON" -m superboard --version)"
if [ "$reported_version" != "superboard $expected_version" ]; then
  echo "smoke: public version mismatch: expected $expected_version, got $reported_version" >&2
  exit 1
fi

# The workspace path is passed explicitly, exactly as a user types it. A first start
# without one is refused by design ("first start needs an explicit workspace path"),
# so an implicit start here would never even reach the assertions below.
(
  cd "$SB_SMOKE_WORKSPACE"
  PATH="$SB_SMOKE_CLEAN_PATH" "$SB_SMOKE_PYTHON" -m superboard --port "$SB_SMOKE_PORT" \
    "$SB_SMOKE_WORKSPACE" >"$SB_SMOKE_LOG" 2>&1
) &
SB_SMOKE_SERVER_PID=$!
smoke_cleanup() {
  kill "$SB_SMOKE_SERVER_PID" 2>/dev/null || true
}
trap smoke_cleanup EXIT

for _ in {1..30}; do
  if /usr/bin/curl --fail --silent "http://127.0.0.1:$SB_SMOKE_PORT" >"$SB_SMOKE_HTML"; then
    break
  fi
  sleep 1
done
/usr/bin/curl --fail --silent "http://127.0.0.1:$SB_SMOKE_PORT" >/dev/null

test -f "$SB_SMOKE_WORKSPACE/inbox/board.md"
test -d "$SB_SMOKE_WORKSPACE/inbox/gc-threads"
test -d "$SB_SMOKE_WORKSPACE/.superboard/journal"
# The first-run board must actually carry the onboarding journey, not just exist.
# Assert the two things the product promises a new user on screen one.
grep -q "^## Getting started" "$SB_SMOKE_WORKSPACE/inbox/board.md"
grep -q "^## My to-dos" "$SB_SMOKE_WORKSPACE/inbox/board.md"
grep -q "Start here" "$SB_SMOKE_WORKSPACE/inbox/board.md"
grep -q "Superboard" "$SB_SMOKE_HTML"

set +e
(
  cd "$SB_SMOKE_COLLISION"
  PATH="$SB_SMOKE_CLEAN_PATH" "$SB_SMOKE_PYTHON" -m superboard --port "$SB_SMOKE_PORT" \
    "$SB_SMOKE_COLLISION"
) >"$SB_SMOKE_COLLISION_LOG" 2>&1
SB_SMOKE_COLLISION_STATUS=$?
set -e
test "$SB_SMOKE_COLLISION_STATUS" -ne 0
grep -q "port $SB_SMOKE_PORT is already in use" "$SB_SMOKE_COLLISION_LOG"
grep -q "superboard --port $((SB_SMOKE_PORT + 1))" "$SB_SMOKE_COLLISION_LOG"

/usr/bin/curl --fail --silent \
  -H 'Content-Type: application/json' \
  -d '{"text":"Verify the missing Claude CLI message"}' \
  "http://127.0.0.1:$SB_SMOKE_PORT/api/quick-capture" \
  >"$SB_SMOKE_CAPTURE"

for _ in {1..30}; do
  if grep -q "Claude binary not found (claude)" "$SB_SMOKE_WORKSPACE/inbox/board.md"; then
    break
  fi
  sleep 1
done
grep -q "Agent run failed: Claude binary not found (claude)" \
  "$SB_SMOKE_WORKSPACE/inbox/board.md"

echo "smoke: passed ($SB_SMOKE_LABEL)"
