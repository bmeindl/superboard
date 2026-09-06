---
name: superboard-update
description: Check Superboard (the installed package) and any installed catalogue skills for updates, explain what is new, install routine updates safely, and negotiate anything that collides with local changes. Use when the "Check for updates" cockpit card runs or the user asks whether Superboard is up to date.
---

# superboard-update

You are updating a tool the user relies on daily. The click on the card IS the order
to install — the default is: update. Bias: **make it reversible first (git), resolve
conflicts yourself, ask only when you genuinely cannot, never overwrite user-owned
files.** How much you decide alone follows this workspace's agent operating rules
(see `.claude/skills/superboard/SKILL.md` and anything the user set during onboarding).

## 1. Probe (read-only)

- Which process serves the board? The workspace's `python3` usually cannot see the
  package at all — the documented `uvx superboard <workspace>` runs it from a
  disposable environment. Find the server through its port (the port in
  `GC_BOARD_URL`, e.g. `http://127.0.0.1:47822`; unset → 47822):
  `lsof -t -iTCP:<port> -sTCP:LISTEN` (Linux without lsof: `ss -ltnp 'sport = :<port>'`)
  → `ps -o command= -p <pid>`. Exactly one pid, or stop and say so. The first word
  is the serving interpreter; keep it, every probe below uses it. Keep the whole
  command line too: it is the restart command.
- Installed package: `<serving python> -c "import importlib.metadata as m; print(m.version('superboard'))"`.
- Latest release: `curl -s https://pypi.org/pypi/superboard/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"`.
  No network → say so plainly and stop.
- Release notes: `RELEASES.md` in the source distribution, or the project's GitHub
  releases when `gh` is authenticated (`gh release view v<latest> -R <repo>`). Read
  `<repo>` out of the PyPI payload's `info.project_urls` — never hard-code it, so a
  moved project still resolves. Fall back to "no notes available" rather than
  inventing a changelog.
- Skills: only if this workspace actually has a catalogue. For each
  `.claude/skills/*/SKILL.md` that records a `source:` and a `version:`, compare it
  with that catalogue's `catalog.json`. No catalogue and no recorded sources → skip
  this part silently; it is not an error.
- How was it installed? Read it off the serving interpreter's path. Check, do not guess:
  - interpreter lives under uv's cache (`uv cache dir`; today `…/archive-v0/…` —
    compare real paths, on macOS `/tmp` is `/private/tmp`) → **uvx**. There is
    nothing to upgrade in place: the update is a restart, `uvx --refresh superboard
    <every argument the running process showed>`, and the rollback is
    `uvx superboard==<old> <same arguments>`. Change only the package selector,
    never drop an argument such as `--port` or `--allow-code-repo`.
  - `uv tool list` names superboard → `uv tool upgrade superboard`.
  - `pipx list` names superboard → `pipx upgrade superboard`.
  - otherwise `<serving python> -m pip install -U superboard`. Never `pip install`
    into an interpreter that is not the one serving the board — that installs a second
    copy the user never runs.

## 2. Report

One compact block: current → latest, what changed (2–5 bullets from the notes), which
of the user's workspace files this could touch (normally none — package code only),
skills with newer versions, catalogue skills not installed. **Everything current → one
line, stop.**

## 3. Local drift

- Package: a reinstall never touches the workspace, so drift only matters if the user
  patched the installed package itself. Check that only when something looks odd.
- Skills: diff the installed `SKILL.md` against its source **at the recorded version**.
  Identical → clean upgrade. Different → a three-way situation; see step 4.

## 3b. Safety net before touching anything

The workspace must be a git repository. Not one yet → `git init` plus
`git add -A && git commit -m "pre-update snapshot"` (local only, no remote needed) —
do it, do not ask. Already a repository → commit or stash the dirty state under a
clear message. Note the commit hash: it is the rollback point for workspace and skill
changes. A package rollback is a reinstall of the previous version.

## 4. Act

- Clean case: upgrade, restart the server the way it was started, then verify — the
  server answers, the board renders, `/api/actions` still lists the same keys as before,
  and the new serving interpreter reports the new package version. Record the old
  version first. Restarting kills the process you are running under? No: runs live in
  their own process group, and a restarted server harvests a finished run's reply from
  `.superboard/journal/`. Do stop the old server and start the new one in ONE detached
  shell that waits for the port to be free, so a half-done restart cannot leave the
  board down and the log survives for diagnosis:
  `nohup sh -c 'kill <pid>; while kill -0 <pid> 2>/dev/null; do sleep 0.2; done; exec <restart command>' >.superboard/restart.log 2>&1 &`
- Drift: resolve it yourself. Apply the upstream change on top of the user's edits,
  keep their intent, run the checks, and commit the result with a message that names
  both sides. Only when the merge is genuinely unresolvable — contradicting intent, or
  the checks still fail after two honest attempts — stop and ask; then start your reply
  with `❓` and show both versions.
- Failure after the upgrade: go back to the previous version the same way you came
  (uvx: restart pinned to `superboard==<old>`; tools: `… install superboard==<old>`),
  restart, and report what failed with the log lines.

## 5. Never

- Write to `actions.json`, `rituals.json`, `board.config.json`, `inbox/board.md`,
  `inbox/gc-threads/`, or the instance configuration block in the board UI.
- Install skills, open issues, or open pull requests without an explicit yes in the
  thread. (Feedback → `gh issue create -R <repo>`; contributions → fork plus
  `gh pr create`. Both only when asked, both need `gh auth status` green.)

## 6. Tell the user what is new

After a successful update, close with a short, friendly "what's new for you" — three to
five bullets in the user's terms (what they can now do, what looks different), drawn
from the release notes and the diff, never a pasted changelog. Name anything they must
do themselves (restart, re-run an onboarding step) explicitly.

## Reply shape

First line, one sentence: `Up to date (0.1.1)` / `Updated 0.1.0 → 0.1.1, verified` /
`❓ 0.1.1 is available but your skill X has local edits — merge proposal below`. Details
after that.
