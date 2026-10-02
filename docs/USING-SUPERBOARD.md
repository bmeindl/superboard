# Using Superboard

This is the operational reference behind the one-minute README. You normally do
not need to read it end to end: the onboarding cards carry the next action, and
the workspace agent can retrieve the relevant section when a question arises.

## Install and start

Superboard is a zero-runtime-dependency Python package. The intended entry is the
coding agent that will later do the delegated work: give Claude Code (Codex on
macOS is experimental) the public repository URL and ask it to inspect the README,
explain the commands, set up `~/Superboard`, start the process and open the local
URL — the README carries that prompt verbatim. By hand, the same thing is:

```sh
uvx superboard ~/Superboard
```

The explicit path is the high-level home from which the agent coordinates work.
On a fresh Git repository root Superboard stops and asks for a higher-level home;
`--allow-code-repo` is the explicit override. Existing workspaces may start the
board from inside their directory with plain `superboard`.

The server listens on `http://localhost:47822`. Superboard reports the workspace
path, Git placement, and Claude Code readiness before creating anything.

The agent-led install is an agent using its existing machine permissions—not a
silent installer built into Superboard. It may still ask before installing `uv` or
opening an application. Without any agent CLI the board still runs as a plain
local to-do board; hand-offs stay off and the first screen says why.

`uvx` resolves the requested package in a disposable environment. To pick up the
latest published release explicitly, run `uvx --refresh superboard ~/Superboard`.
There is no persistent app install to remove: stop the process and optionally run
`uv cache clean superboard` to clear uv's cached package data. Superboard never
deletes the workspace you named. That workspace—including `inbox/board.md`, its
threads, and your configuration—is your data, so deleting it deletes your data.

The default Claude Code runner is supported on macOS and Ubuntu. The Codex runner
is experimental and currently follows ChatGPT's macOS application path. OpenCode
is not yet a supported runner; onboarding must not present it as available.
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

Starter copies are yours the moment you edit them: starting or upgrading Superboard
never replaces a file you changed. A starter skill you never touched follows the
release you run (`.superboard/starter-stamps.json` remembers what was seeded). The
shipped **Check for updates** card is added once to an `actions.json` that never had
it; delete it and it stays gone (the offer is remembered in `.superboard/`, so
wiping that directory repeats it once). A `.gitignore` for `.superboard/` is seeded when
the workspace has none.

## First-run journey

A fresh browser opens the normal To-dos view. My to-dos sits on top, empty or holding
the few tasks the installing agent brought in with the user's OK; Getting started sits
below it with eight numbered cards. Getting started never counts toward the NEW/NOW
meters, the 'Now' limit or the board's load, and a card added without a topic lands in
My to-dos, never in the checklist. Each onboarding title names the outcome:

| Now | Next | Backlog |
| --- | --- | --- |
| 1 · Start here · Meet Superboard | 4 · Understand runs, threads and cache | 8 · Finish Getting started |
| 2 · Hand off your first real task | 5 · Find settings and get help | |
| 3 · Set up this workspace | 6 · Set up your Cockpit | |
| | 7 · Get more from Superboard | |

The first session is shaped by the installing agent (README, "For your agent"): it
fits the board into an existing workspace or a small new one and may add up to three
accepted tasks to My to-dos. Card 2 then either picks one of those or asks for one real
task, and shows the first hand-off. Card 3 only matters when the installer did not
already set the workspace up; otherwise it summarizes what exists and closes.

Opening a card spends no model tokens. Card 1 asks the user to press `▶ Agent` once;
that round opens the same-origin introduction at `/welcome` and keeps follow-up
questions in the card. Card 4 explains runs, threads, new sessions and cache using its
own task as the example and links the illustrated version at
`/onboarding-showcase#threads`. Card 7 is optional inspiration: it opens
`/inspiration`, which explains what the Cockpit is and shows four things people rarely
think to ask for, and lists setups to request later in any card (email digest, one
routine, an off-duty view, night rest, learning from threads after a few days). `✓ Done` completes a card; the checkbox remains available for immediate undo
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

## Writing into the board from outside

Superboard has one writer: the running server. Nothing else edits `inbox/board.md`
— not you, not an agent, not a script. Everything that wants to write goes through
the server, which is what makes a card safe to post into from several places at
once. The two doors:

- `.superboard/board_write.py` — the client the server copies into every workspace.
  Pure standard library, runs with any `python3`, no install. `--help` lists the
  whole sanctioned surface: `--show` (body + revision), `--body-file` +
  `--body-etag` (replace a body, optimistic locking), `--stage` (process stage),
  `--new-card` / `--ensure-card` (create a to-do; never starts a run), `--new-topic`,
  `--docs`. Column names are `Now`, `Next`, `Backlog`. The client talks to
  `http://127.0.0.1:47822`; a board on another port takes `--url` or `GC_BOARD_URL`.
- `POST /api/gc-append` — the endpoint the runner itself uses to report back.
  JSON body `{"kind": "reply", "by": "agent", "text": "…", "addr": {"id": "<gc-id>"}}`.
  `ask` addresses the agent, `reply` returns an answer, `done` closes the thread,
  and `sys` adds context that answers nothing. Send `by` separately to identify authorship. The
  `@gc-id` is the 12-hex tag on the card (visible in the file and in the card's
  overlay). The server answers 409 if the card is not uniquely found.

Typical uses: a cron job posting a nightly result into the card that owns it, a CI
step reporting a deploy, or a different agent handing something over to the board
instead of to a chat window. The file stays the review surface either way.

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


## Turn authorship and optional experiments

Thread direction and authorship are separate. An `ask` can be written by a person or an
agent; integrations should send `"by":"agent"` for generated asks. Turns may also carry
a timestamp and the model reported by the runner. Older turns without enough evidence
are shown as author unverified, not silently attributed to you.

Jev review, reply suggestions and automatic thread-cut advice are not included in 0.4.0.
No separate classifier service or credentials are needed. The optional item terminal
requires `tmux` and `ttyd`; normal task threads work without those executables.
