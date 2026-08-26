#!/bin/sh
# Superboard test rig — look at the product the way a first-time user does.
#
#   scripts/testrig.sh fresh    the whole point in one word: stop whatever rig is
#                               running, wipe its workspace, build, install, serve —
#                               what you see is a first-time install (blocks)
#   scripts/testrig.sh up       build a wheel from THIS repo, install it into a
#                               clean workspace and serve it (blocks; Ctrl-C stops)
#   scripts/testrig.sh stop     stop a running rig (only a rig — see below)
#   scripts/testrig.sh reset    delete the rig workspace — back to a fresh download
#   scripts/testrig.sh status   where the rig is, what is listening where
#
# `fresh` exists because that is the only sequence anyone actually wants, and
# getting it wrong is silent: an `up` on top of a used workspace looks like a
# first install and is not one. It is also what a one-click button can call.
#
# The rig deliberately installs from a BUILT ARTIFACT (dist/*.whl) via
# `uvx --no-cache`, not from the source tree: that is the thing a stranger
# downloads, and --no-cache stops uv from serving an older build of the same
# version number.
#
#   RIG_DIR    where the rig workspace lives   (default <repo>/.testrig)
#   RIG_PORT   port the rig binds              (default 47850)
#
# The rig workspace lives INSIDE the repo folder, gitignored, next to sandbox/:
# one Superboard folder on disk instead of a repo plus a stray sibling directory.
# It is still a separate WORKSPACE (own board, own threads, own runtime data) —
# the installed wheel never reads the source tree it was built from. Point RIG_DIR
# somewhere else if you want the rig off the repo disk entirely.
#
# Ports are pinned on purpose. 47822 is what a normal install takes, 47899 is
# scripts/sandbox.sh — a rig on either would either fail to start or, worse, show
# you a DIFFERENT board than the one you think you are looking at.
#
# --------------------------------------------------------------------------
# What the rig isolates, stated honestly:
#
#   Files    yes — its own workspace, its own board, its own threads, its own
#            runtime data. Nothing is read from or written to another workspace.
#   Port     yes — pinned and distinct; a collision now fails with one readable line.
#   Agent    context only. Runs go through tools/claude-identities/claude-private
#            (an extension point the runner already honours), which drops the
#            operator's user-scope settings, skills, MCP servers and plugins, so
#            the agent reasons like a stranger's fresh install and not like a
#            long-configured one. Measured: ~19.3k vs ~14.1k prompt tokens.
#   Filesystem  NO. This is a folder, not a sandbox. A Claude run started in the
#            rig can still READ the entire home directory, including private
#            workspaces. If you need to PROVE an agent cannot reach something,
#            you need a container — a directory cannot give you that.
# --------------------------------------------------------------------------
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RIG="${RIG_DIR:-$REPO/.testrig}"
PORT="${RIG_PORT:-47850}"
MARKER=".superboard-testrig"      # only a directory carrying this is ours to delete

# Guard rails for a directory this script will later `rm -rf`: never the repo
# itself, never a home or root directory, and inside the repo only the one
# gitignored path we own (.testrig) — not, say, the package or sandbox folder.
case "$RIG" in
  "$REPO/.testrig" | "$REPO/.testrig"/*) : ;;
  "$REPO" | "$REPO"/*)
    echo "testrig: inside the repo only $REPO/.testrig is allowed (got $RIG)" >&2; exit 1 ;;
  "$HOME" | "$HOME"/ | /)
    echo "testrig: refusing to use $RIG as a rig directory" >&2; exit 1 ;;
esac

# The rig identity: what the runner spawns instead of a bare `claude`.
# gc_runner picks this path up by itself (PRIVATE_CMD) when it exists under the
# workspace root — no product code is patched to make the rig work.
write_identity() {
  mkdir -p "$RIG/tools/claude-identities"
  cat > "$RIG/tools/claude-identities/claude-private" <<'WRAPPER'
#!/bin/sh
# Test-rig identity — approximate a stranger's Claude install.
#   --setting-sources project,local   ignore the operator's ~/.claude/settings.json
#                                     (its hooks and statusline point into a private repo)
#   --strict-mcp-config               no MCP servers except ones passed explicitly (none)
#   --disable-slash-commands          none of the operator's skills
# Deliberately NOT set: CLAUDE_CONFIG_DIR. Pointing it anywhere relocates the
# account file and the CLI comes up logged out — see superboard/claude_identity.py.
# The rig shares the account; it must not share the context.
exec claude --setting-sources project,local --strict-mcp-config --disable-slash-commands "$@"
WRAPPER
  chmod +x "$RIG/tools/claude-identities/claude-private"
}

listening() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

case "${1:-up}" in

  up)
    command -v uvx >/dev/null 2>&1 || { echo "testrig: uvx not found — install uv first" >&2; exit 1; }
    if listening "$PORT"; then
      echo "testrig: port $PORT is already taken — a rig may still be running." >&2
      echo "         Stop it, or start elsewhere:  RIG_PORT=$((PORT + 1)) $0 up" >&2
      exit 1
    fi

    echo "testrig: building a wheel from $REPO"
    rm -rf "$REPO/dist"
    ( cd "$REPO" && uv build --wheel -q )
    WHEEL="$(ls "$REPO"/dist/*.whl | head -1)"
    [ -n "$WHEEL" ] || { echo "testrig: no wheel was built" >&2; exit 1; }

    mkdir -p "$RIG"
    printf 'Superboard test rig. Created by scripts/testrig.sh — safe to delete.\n' > "$RIG/$MARKER"
    write_identity

    echo "testrig: installing $(basename "$WHEEL") into $RIG"
    echo "testrig: http://localhost:$PORT   (Ctrl-C stops it, 'testrig.sh reset' empties it)"
    cd "$RIG"
    exec uvx --no-cache --from "$WHEEL" superboard "$RIG" --port "$PORT"
    ;;

  stop)
    if ! listening "$PORT"; then
      echo "testrig: nothing is listening on $PORT"
      exit 0
    fi
    # Kill by argv, not by port: every rig process carries the pinned port in its
    # command line, so this can never take down a stranger that happens to sit on
    # the same socket. If it is not ours, say so and leave it alone.
    if ! pgrep -f "superboard .* --port $PORT" >/dev/null 2>&1; then
      echo "testrig: port $PORT is held by something that is not a Superboard rig — leaving it alone." >&2
      exit 1
    fi
    pkill -f "superboard .* --port $PORT" || true
    n=0
    while listening "$PORT" && [ "$n" -lt 25 ]; do sleep 0.2; n=$((n + 1)); done
    if listening "$PORT"; then
      pkill -9 -f "superboard .* --port $PORT" || true
      sleep 0.5
    fi
    if listening "$PORT"; then
      echo "testrig: port $PORT is still held after SIGKILL" >&2
      exit 1
    fi
    echo "testrig: stopped — port $PORT is free"
    ;;

  fresh)
    sh "$0" stop
    sh "$0" reset
    exec sh "$0" up
    ;;

  reset)
    if [ ! -d "$RIG" ]; then echo "testrig: nothing to reset ($RIG does not exist)"; exit 0; fi
    if [ ! -f "$RIG/$MARKER" ]; then
      echo "testrig: $RIG has no $MARKER file — refusing to delete a directory the rig did not create." >&2
      exit 1
    fi
    if listening "$PORT"; then
      echo "testrig: a rig still holds port $PORT — stop it before resetting." >&2; exit 1
    fi
    rm -rf "$RIG"
    echo "testrig: $RIG removed — the next 'up' is a first-time install again"
    ;;

  status)
    echo "repo:  $REPO"
    echo "rig:   $RIG $([ -d "$RIG" ] && echo '(exists)' || echo '(not created yet)')"
    if [ -f "$RIG/inbox/board.md" ]; then
      echo "board: $RIG/inbox/board.md ($(wc -l < "$RIG/inbox/board.md" | tr -d ' ') lines, \
$(ls "$RIG/inbox/gc-threads" 2>/dev/null | wc -l | tr -d ' ') thread files)"
    fi
    for p in 47822 47899 "$PORT"; do
      case $p in
        47822) what="a normal install" ;;
        47899) what="scripts/sandbox.sh" ;;
        *)     what="this rig" ;;
      esac
      printf '%s  %-22s %s\n' "$p" "$what" "$(listening "$p" && echo 'LISTENING' || echo 'free')"
    done
    ;;

  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 1 ;;
esac
