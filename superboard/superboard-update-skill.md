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

- Installed package: `python3 -c "import importlib.metadata as m; print(m.version('superboard'))"`.
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
- How was it installed? `uv tool list` → `uv tool upgrade superboard`; `pipx list` →
  `pipx upgrade superboard`; otherwise `pip install -U superboard`. Check, do not guess.

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
  server answers, the board renders, `/api/actions` still lists the same keys as before.
  Record the old version first.
- Drift: resolve it yourself. Apply the upstream change on top of the user's edits,
  keep their intent, run the checks, and commit the result with a message that names
  both sides. Only when the merge is genuinely unresolvable — contradicting intent, or
  the checks still fail after two honest attempts — stop and ask; then start your reply
  with `❓` and show both versions.
- Failure after the upgrade: reinstall the previous version
  (`… install superboard==<old>`), restart, and report what failed with the log lines.

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
