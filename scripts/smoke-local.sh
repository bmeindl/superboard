#!/usr/bin/env bash
# The local half of the macOS smoke test — the counterpart CI runs.
#
# Run THIS, not scripts/smoke-installed.sh directly. smoke-installed.sh asserts
# against whatever Superboard the interpreter already has; on a machine where
# dist/ survives between sessions that can be a wheel from an older commit, and
# it would pass without a word. This script rebuilds first and stamps the commit
# it built from, so the assertions provably run against the current checkout.
#
# Same steps as the "Build and install the wheel" job in
# .github/workflows/macos-smoke.yml, deliberately kept short enough to compare.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"

python3 -m pip install --upgrade pip >/dev/null
rm -rf dist
python3 -m pip wheel . --wheel-dir dist

wheels=(dist/superboard-*.whl)
if [ "${#wheels[@]}" -ne 1 ]; then
  echo "smoke-local: expected exactly one wheel in dist/, found ${#wheels[@]}" >&2
  printf '  %s\n' "${wheels[@]}" >&2
  exit 1
fi

git rev-parse HEAD > dist/BUILD_SHA
python3 -m pip install --force-reinstall "${wheels[0]}"
echo "smoke-local: built $(basename "${wheels[0]}") from $(cat dist/BUILD_SHA)"

exec scripts/smoke-installed.sh
