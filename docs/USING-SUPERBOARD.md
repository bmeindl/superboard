# Using Superboard

This is the operational reference behind the one-minute README. You normally do
not need to read it end to end: the onboarding cards carry the next action, and
the workspace agent can retrieve the relevant section when a question arises.

## Install and start

Superboard is a zero-runtime-dependency Python package:

```sh
uvx superboard ~/Superboard
```

The explicit path is the high-level home from which the agent coordinates work.
On a fresh Git repository root Superboard stops and asks for a higher-level home;
`--allow-code-repo` is the explicit override. Existing workspaces may start the
board from inside their directory with plain `superboard`.

The server listens on `http://localhost:47822`. Superboard reports the workspace
path, Git placement, and Claude Code readiness before creating anything.

If Claude Code is already installed and signed in, the user can instead give it
the public repository URL and ask it to inspect the README, explain the commands,
set up `~/Superboard`, start the process, and open the local URL. That is an agent
using its existing machine permissions—not a silent installer built into
Superboard. It may still ask before installing `uv` or opening an application.

`uvx` resolves the requested package in a disposable environment. To pick up the
latest published release explicitly, run `uvx --refresh superboard ~/Superboard`.
There is no persistent app install to remove: stop the process and optionally run
`uv cache clean superboard` to clear uv's cached package data. Superboard never
deletes the workspace you named. That workspace—including `inbox/board.md`, its
threads, and your configuration—is your data, so deleting it deletes your data.

The default Claude Code runner is supported on macOS and Ubuntu. The Codex runner
is experimental and currently follows ChatGPT's macOS application path. OpenCode
is not a supported runner in `0.1.0`; onboarding must not present it as available.
Windows has not passed the release smoke and is not supported in this alpha.

## Workspace-owned files

```text
inbox/board.md                         topics, to-dos, and threads
actions.json                           one-click cockpit jobs
rituals.json                           optional daily/weekly prompts
board.config.json                      local labels and optional behavior switches
.claude/skills/superboard/SKILL.md     the agent's workspace-admin guide
.claude/skills/superboard-update/SKILL.md   the skill the update card loads
superskills/                           optional separate catalogue checkout
.superboard/                           runtime journals and disposable caches
```

Starter copies are create-only. Starting or upgrading Superboard never replaces
a file the user already owns.

## First-run journey

A fresh browser opens the normal To-dos view with separate Getting started and
My to-dos categories. Getting started has fourteen numbered cards; My to-dos is
empty and ready for ordinary work. Each onboarding title names the outcome; there is no generic
"optional setup" gate hiding several unrelated jobs:

| Now | Next | Backlog |
| --- | --- | --- |
| 1 · Start here · Meet Superboard | 8 · Set up an email digest | 14 · Finish Getting started |
| 2 · Set up this workspace | 9 · Set up one routine | |
| 3 · Add your first real to-do | 10 · Set up an off-duty view | |
| 4 · Understand runs, threads and cache | 11 · Turn on night rest | |
| 5 · Find settings and get help | 12 · Let Superboard learn from your threads | |
| 6 · Check your agent and model setup | 13 · Get more from Superboard | |
| 7 · Set up your Cockpit | | |

Opening a card spends no model tokens. Card 1 asks the user to press `▶ Agent` once;
that round opens the same-origin introduction at `/welcome` and keeps follow-up
questions in the card. Card 4 explains runs, threads, new sessions and cache using its
own task as the example and links the illustrated version at
`/onboarding-showcase#threads`. Card 13 is optional inspiration: it opens
`/inspiration`, which explains what the Cockpit is and shows four things people rarely
think to ask for. `✓ Done` completes a card; the checkbox remains available for immediate undo
until reload. Setup cards are guidance, not gates, except that the final cleanup
card requires the other cards to be completed or consciously skipped. It then
archives their cards and threads before removing the topic.

Set up this workspace stays about its path, boundaries, context foundation and
board topics. It also makes the workspace a git repository when it is not one yet —
`git init` plus a first commit, decided and done rather than asked, so every later
change stays diffable and revertible. It neither audits agent CLIs nor connects integrations. In a narrow
repository that already contains work, it must explain that a restart elsewhere
does not move cards, threads or spend history and present an approved recovery plan.
Agent and model readiness has its own card. It confirms the platform and run
profile that already succeeded, then adds another platform only when the user
has a concrete reason. Email digest, one routine and later thread-learning are
separate cards, so each can be completed or consciously skipped on its own.

The optional Off Duty setup stores exact hidden and visible topic names in
`board.config.json` only after approval. The toggle changes the local projection,
not the board; unclassified and newly created topics remain visible.

The Cockpit tab is there from the first start, holding exactly one shipped action:
⬆️ Check for updates. Clicking it is the order to install — an agent compares the
installed package with the latest release, makes the workspace a git repository and
commits it first so the step is revertible, merges the update with local changes
itself, asks only when a conflict is genuinely unresolvable, and closes with a short
note on what is new. It never writes to `actions.json`, `rituals.json`,
`board.config.json`, `inbox/board.md` or the thread files. Because that one card
exists, a fresh Cockpit shows one populated zone rather than five empty ones; delete
every action and the tab disappears again.

Card 7 therefore customizes an existing Cockpit rather than creating one. It sits in
Now after the workspace has context. The base-setup round first creates one idempotent
extension card, then proposes 2–4 actions whose skills, CLIs, and authorization
boundaries it has actually verified. The visual tour includes fictional maintenance,
knowledge, and personal-sports Cockpits; it never seeds those examples or their data
into the workspace.

Optional catalogue skills are never bundled, installed, or updated silently.
The agent previews the selected skill, copies it into the workspace-owned skill
folder, and can adapt its local policy. Cards contain enough fallback guidance
to work without the catalogue.

## Adding and changing work

- `Enter` in `+ New…` writes a plain card.
- `Cmd/Ctrl+Enter` writes the card and starts its agent thread.
- The quick-capture bar follows the same hand-off model.
- Ask the workspace agent to add or change topics, actions, rituals, and skills;
  it follows the boundaries in `.claude/skills/superboard/SKILL.md`.

Superboard ships the ritual machinery — a daily or weekly prompt in the footer,
an optional full-screen gate when one is overdue, and a journal of what you
answered — but it ships no rituals of its own. The starter contains no borrowed
actions or rituals. `rituals.json` starts empty so a new user is not greeted by
somebody else's overdue routine. Ask your agent for the first one when you know
what you want to be asked regularly.

Actions and rituals are re-read after a page reload. Changes to
`board.config.json`, `board.contract.md`, or package code need a process restart.
Stop the process with `Ctrl+C` and run the same start command again; `inbox/`
holds the durable state, so a restart loses no board work.

## Agent access and handoffs

The selected CLI runs in auto mode and inherits its host access plus configured
MCP/provider setup. It may edit and commit in the workspace, and a task may call
for machine-wide or outside-workspace work. Superboard adds no parallel approval
layer: state boundaries in the card, inspect files and diffs, and ask the agent for
exact terminal handoff commands whenever login or other interaction is required.

The late-night rest ladder is available but off by default. To opt this workspace
into its wind-down pill, reminders, and mandatory 23:00–06:00 pause, set
`"night_pause": {"enabled": true}` in `board.config.json` and restart Superboard.

## Sessions, memory, and cache

Every hand-off launches a new CLI process. A card can continue its conversation
because Superboard stores a session handle and asks the selected provider to
resume its transcript. The board thread remains the portable source of truth: if
the runner changes or the transcript disappears, the next run starts fresh from
the board context.

Provider prompt caching is separate. It can reuse an unchanged input prefix to
save tokens and sometimes latency. A cold cache means more input processing, not
lost cards, answers, files, or memory. Card-level time indicators are recency
cues; the thread overlay contains the narrower runner-specific measurement.

## Known limitations of the board file

The board is one markdown file, and everything — the UI, the CLI helpers, an
agent writing back a result — reads it, changes it, and writes it out again. That
is what keeps the state inspectable and portable. It also means the file's own
conventions are the only thing holding item identity together, and two gaps in
that are known and deliberately left open rather than papered over.

**Editing `board.md` by hand can silently give an item a new identity.** Each item
carries a `@gc-id:` line. That id is what the item's thread file, its sub-items,
and its run lock are keyed on. If a hand edit drops that line, nothing can tell
"this item never had an id" from "this item just lost its id" — the text looks
identical. Superboard makes a best-effort recovery (if a recent thread file
carries exactly this item's title and points at an id nothing else claims, the
item gets that id back), and it says on stderr when it could not, naming where
the now-orphaned thread sits. But a recovery that depends on the title cannot
work when the same edit also changed the title. Prefer the UI or the CLI helpers
for edits; if you do edit by hand, keep the `@gc-id:` lines.

**Under Claude Code, a duplicate can be caught the moment it is written.**
`guard_hook.py` re-runs `board_lint.py` after every Edit/Write/Bash and reports a
fresh structural duplicate straight back into the agent's own context, so the run
that caused it is the run that sees it. It is not wired in by default: add a
`PostToolUse` entry to this workspace's `.claude/settings.json` pointing at
`python3 -m superboard.guard_hook`, with the matcher
`Edit|Write|MultiEdit|NotebookEdit|Bash|Task`. It fails open, costs nothing on an
unchanged board, and `GC_BOARD_GUARD=off` disables it outright.

**Duplicate ids are reported, not blocked.** `board_lint.py` flags two items
sharing a `@gc-id`, but no write path refuses to save one — every lookup takes the
first match, so the copies drift apart quietly. Copying an item block by hand is
the usual way to get there. Run the linter after hand edits.

Both are the same underlying shape: the file is the source of truth, so a guard
strict enough to prevent this would also be strict enough to lock you out of your
own board. That trade was made once in the other direction and reverted.
