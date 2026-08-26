# Architecture

Superboard is a single local Python package with no runtime dependencies: a
small HTTP server renders and edits one markdown file (`inbox/board.md`), and
a headless runner drives a Claude Code (or Codex) subprocess per card. There
is no database and no account — the markdown file plus a handful of local
JSON registries and caches are the entire durable state. This document
covers the invariants and trust boundaries that don't change from run to
run; day-to-day facts (current version, open issues, in-flight decisions)
belong in the CHANGELOG or the board itself, not here — keeping temporary
facts out of this file is what keeps it worth reading.

## Components

- `server.py` — the local single-writer HTTP server: serves the board UI,
  parses/renders `board.md`, and exposes the JSON API the frontend and the
  runner both call.
- `gc_runner.py` — the headless agent runner: picks up a `@gc:` board item,
  assembles its prompt (contract + working state + thread), and drives a
  Claude Code / Codex subprocess to a reply.
- `contract.py` — composes the agent's completion contract from generic core
  rules plus an optional instance-specific rule file (`board.contract.md`,
  not shipped in this extraction).
- `sidecar.py` — long-turn externalization: writes oversized thread turns to
  `inbox/gc-threads/` and leaves a short pointer in the thread.
- `sweep.py` — retention: moves finished (`done`) items out of the live
  board into the archive on a schedule.
- `registries.py` — fail-soft loaders for `actions.json` (cockpit action
  cards) and `rituals.json` (recurring ritual definitions).
- `thread_search.py` — cross-run context retrieval: lexical search over
  board/archive/thread history with a bounded, evidence-only selection.
- `terminal.py` — read-only web view of an item's live agent session, plus
  the `resume` command table per runner lane.
- `bump.py` — mechanical version bump (patch/minor/major from commit size),
  paired with the CHANGELOG-sync convention checked by the test suite.
- `receipt.py` / `receipt_hook.py` — machine-readable per-run fact log and
  its optional extension boundary.
- `retro_scan.py` — deterministic candidate finder for error retrospectives
  over past runs.
- `dev_radar.py` — live status rollup of open dev topics on the board.
- `board_lint.py` — fast diagnostic for why the board file is locked or
  malformed, including duplicate ids and duplicate topic headings.
- `guard_hook.py` — its optional Claude Code PostToolUse companion: after a
  write it re-lints and reports a fresh structural duplicate back into the
  agent's own context. Opt-in; see `docs/USING-SUPERBOARD.md`.
- `board_write.py` — the agent-facing board client, and the whole sanctioned
  write surface: replace an item body against a revision token, append a
  process stage, create a to-do, create a topic, or read the product's own
  docs. It is deliberately pure standard library and imports nothing from this
  package, because `_bootstrap` copies it into every workspace at
  `.superboard/board_write.py` and the agent's shell is a separate process that
  may not be able to import `superboard` at all. Prompts name that workspace
  path, never `python3 -m superboard.board_write`. Unlike the user-owned
  starter files it is refreshed on every start: it is product mechanics, and an
  upgraded server must not leave an agent holding an older client.
  Its workspace path stays `.superboard/board_write.py` even when `GC_DATA`
  redirects journals and caches elsewhere. Every spawned runner receives the
  active server URL as `GC_BOARD_URL`, so the same command works on `--port`
  overrides instead of silently falling back to 47822.
- `board_ls.py` — quick agent-facing overview of board contents.
- `paths.py` / `config.py` — the one place that resolves where the board's
  data lives and what is instance configuration versus mechanic.
- `markers.py` — the frozen data-format strings persisted in `board.md`
  (thread tags, sidecar references, protocol prefixes) — never translated,
  never renamed casually.
- `git_state.py` — git status/delta used by the runner prompt, independent
  of optional telemetry.
- `claude_identity.py` — explicit, non-secret identity boundaries for board
  subprocesses (which binary/account lane runs, and why).
- `migrate_diet.py` — one-time `board.md` migration helper.

## Package and workspace ownership

The installed package is read-only mechanics. On first start the user names the
mutable instance explicitly (`superboard <workspace>`); an existing workspace
may still be served by running plain `superboard` from inside it. Before creating
files, the console reports the resolved path, a containing Git repository, a
non-fatal Claude installation/authentication check, and the inherited host/config
trust boundary of auto-mode runs. A fresh Git-repository root
is refused unless `--allow-code-repo` records the user's explicit intent. On
every console start, the bootstrapper creates missing workspace files with
exclusive-create semantics and never overwrites an existing one:

- `inbox/board.md` and `inbox/gc-threads/` — board content and conversations;
- `actions.json` and `rituals.json` — repeatable jobs and recurring prompts;
- `board.config.json` and optional `board.contract.md` — instance identity,
  opt-in behavior and additional agent rules;
- `.claude/skills/superboard/SKILL.md` — the workspace administration guide;
- `.superboard/` — runtime journals, usage data, and caches.

`paths.py` is the single source of truth for all of these locations. Product
assets such as `index.html`, the Python modules, and the generic starter sources
remain in the package. `actions.json` and `rituals.json` are read fresh by their
API paths; owner configuration, opt-in night-rest behavior and the rendered agent
contract are imported at process start and therefore require a restart after
changes. The whole night-rest ladder (footer pill, reminders and mandatory pause)
defaults off and is enabled only by a literal `night_pause.enabled: true` in the
workspace config.

This boundary is also the upgrade contract: installing a newer wheel may change
mechanics and generic starter sources, but it must not replace the user's board,
actions, rituals, configuration, contract, or skills.

First-run onboarding is data on that same boundary, not a separate state machine.
Only a missing `board.md` produces two neutral topics: a seeded Getting started
checklist and an empty My to-dos area that makes ordinary work visible from frame one.
Each onboarding card has its own id and one pending user turn. The first prepared
round opens the same-origin introduction; the UI itself injects no card-specific
tour button. Runs, threads and cache are taught as a concrete agent-led to-do in
plain language. The sequence then establishes the workspace boundary/context,
creates one genuine normal card, explains settings/help, confirms the already-working agent/model profile, configures an explicit Off Duty
projection and reveals the Cockpit payoff. Email digest, one routine and later
thread-learning remain separate ordinary cards: optionality is expressed by completing
or consciously skipping a concrete outcome, not by a generic chooser that creates more
cards. The Backlog closer can finish only when the other cards are done;
its normal Done path first closes the thread, then
atomically archives the topic's item blocks and moves their sidecars before removing the
Getting started topic. The packaged action and
ritual registries stay empty. A browser with no saved tab choice lands on To-dos while every seeded card
is still unstarted, so the checklist is visible. Restarting does not recreate it,
and after the first render the ordinary persisted tab choice wins.

The Cockpit is capability-revealed, not an empty product shell. `/api/actions` is
loaded before tab selection; zero valid actions means no Cockpit tab, while a
configured Cockpit renders only zones containing actions. Base setup must first
idempotently ensure one extension card, inventory only non-secret capability
metadata, and obtain approval before surgically editing `actions.json`. The same-origin
`/onboarding-showcase` is packaged fictional data: it can explain customization
without leaking or seeding the maintainer's personal or work data.

Scheduled triage is fail-soft and gets one automatic attempt per slot. Its model
contract uses a flat JSON object; conservative closing-bracket repair handles a
truncated envelope without inventing entries. A failed reply never replaces the last
good snapshot, is retained as `journal/triage-last-raw.txt`, and blocks another
automatic attempt until the next slot (manual refresh remains available).

## Invariants and trust boundaries

- `board.md` has a single writer (the local server) — the runner and the UI
  only ever go through its API, never edit the file directly. Agents are held
  to the same rule by giving them a client that always works rather than only a
  prohibition: an agent told "never edit board.md" whose only sanctioned tool
  fails will do the helpful thing and edit the file, and every later run reads
  that thread and learns the hand edit is normal.
- Dynamic onboarding cards use the workspace client `--ensure-card`, which checks
  exact active titles before creating. This makes interruption/resume idempotent;
  in particular Cockpit base setup cannot duplicate or lose its extension card.
- The product's own documentation is SERVED, never copied into a workspace.
  `GET /api/docs/{readme,architecture,changelog}` reads it from the running
version; a copied doc goes stale on the next upgrade, and onboarding cards
  that read one would then teach from a stale source.
- The runner preflight (is Claude Code installed and signed in?) has one
  implementation, `server.runner_status`, used by both the terminal preflight
  and `GET /api/runner-status`. The browser must be able to say why ▶ Agent
  cannot run; a terminal line the user never saw is not an explanation.
- Off Duty is an explicit view projection from
  `board.config.json.off_duty.{hidden_topics,visible_topics}`. Only exact hidden
  names are filtered, so unknown and newly created topics remain visible; no card
  is moved, completed, or rewritten.
- Binding to `127.0.0.1` is not an authentication boundary. A browser is a local
  program too, so any page the user visits can reach the server. `Content-Type:
  text/plain` is a CORS simple request and travels without a preflight, so a
  foreign page could once create cards and start agent runs — a code-execution
  path, not merely a write. Every state-changing request now passes a guard
  first: `Sec-Fetch-Site: cross-site` is refused, an `Origin` that is not this
  server on this port is refused, and a request carrying a body must declare
  `application/json`. Requests with no `Origin` at all stay allowed — that is
  what every local, non-browser tool looks like — and reads are untouched.
- The lost guards protect LINES, not IDENTITY. They compare known line families
  before a write; free text is not a family, so a line the parser drops silently
  is invisible to them. That is why the parser keeps mis-indented lines (one
  space, a tab) as body instead of discarding them, and why losing an item's
  `@gc-id` to a hand edit is recovered best-effort rather than prevented: from
  the text alone, "never had an id" and "just lost its id" are the same string.
  Duplicate ids are linted and reported, never blocked. See the known-limitations
  section of `docs/USING-SUPERBOARD.md` — this is a documented boundary of the
  plain-file model, not an oversight.
- Protocol markers in `markers.py` (`@gc:`/`@gc-re:`/`@gc-done:`, sidecar
  reference labels, the compact/handoff prefixes) are a persisted data
  format, not UI copy — they stay byte-stable across languages and rewrites.
- The agent contract (`contract.py`) always renders a safe core even with no
  workspace `board.contract.md` present; an instance file, if present, must be
  well-formed or startup fails loudly rather than silently degrading.
- Bootstrap is create-only for user-owned workspace files. Restart and package
  upgrade never overwrite existing instance content. The bundled board client is
  refreshed in place because it is product mechanics, not user content; redirecting
  runtime data never moves that advertised command path.
- Setup steps are normal pending threads and never auto-run. Process start itself
  spends no agent tokens; the owner must explicitly click `▶ Agent` per step.
- Completing from the card overlay and completing from the matrix share the same
  `toggleDone()` path. Both honor the ritual gate, persist the completion timestamp,
  close an open thread, and keep a just-completed card visible until reload for undo.
- The Getting started closer is the one deliberate exception to that last visibility
  behavior: after the canonical Done/thread-close path it archives the complete topic and
  removes it. It refuses while any of the other seven cards is open, and history reaches
  `board-archive.md` before the active topic disappears.
- A runner session and a provider prompt cache are different state. The session handle in
  `board.md` can resume a vendor transcript; prompt caching only reuses repeated input.
  Either may be absent without losing board truth, because fresh runs are rebuilt from the
  local thread and item context.
- A fresh browser starts on the Claude CLI's own default model (no `--model`
  override). Optional and alternative profiles are explicit per-user choices;
  unsupported runners are never presented as if they were already configured.
- Subprocess identity (`claude_identity.py`) is explicit and never inherited
  silently from parent-process environment variables.
- The standalone package starts no scheduled agent work. Runs begin with an
  explicit board/UI action; background scheduling is instance configuration,
  not a v0 default.
- Codex receives no MCP server configuration or credential variables in v0.
  Integrations need an explicit, reviewed boundary before they can ship.
- The local file viewer blocks `personal/`, `private/`, dot-directories,
  environment files, unknown suffixes, oversized files and paths outside the
  active workspace.
- A board process binds its port BEFORE it starts the journal watch. The watch
  talks to `127.0.0.1:<port>`; started first, a second instance on an occupied
  port would post its orphaned runs into the board that already owns that port.
  Binding first means the port is ours or the process is already gone.
- Workspace isolation is a file boundary, not a sandbox. Two workspaces never
  share a board, threads or runtime data, and the runner's identity wrapper can
  strip the operator's Claude configuration from a run — but an agent started in
  a workspace still has the read access of the user who started it.
