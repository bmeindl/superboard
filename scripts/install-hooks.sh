#!/bin/sh
# One command, once per clone:  sh scripts/install-hooks.sh
#
# Git hooks live in .git/hooks, which is deliberately NOT versioned — so a repo
# cannot ship a hook that runs on your machine without you saying so. The way
# around that is core.hooksPath: the hooks themselves live in scripts/hooks/ and
# are reviewable in the diff like any other file, and this one line points git at
# them. That one line is per-clone and cannot be committed, which is the point.
#
# Idempotent. Run it again any time; it prints what it did.

set -e
repo="$(git rev-parse --show-toplevel)"
cd "$repo"

chmod +x scripts/hooks/* 2>/dev/null || true

current="$(git config --get core.hooksPath || true)"
if [ "$current" = "scripts/hooks" ]; then
	echo "hooks already installed (core.hooksPath = scripts/hooks)"
else
	git config core.hooksPath scripts/hooks
	echo "hooks installed: core.hooksPath = scripts/hooks"
	[ -n "$current" ] && echo "  (replaced previous setting: $current)"
fi

echo "  pre-commit  leak scan over the working tree"
echo "  pre-push    leak scan over the commits being pushed"
echo
echo "Uninstall with:  git config --unset core.hooksPath"
