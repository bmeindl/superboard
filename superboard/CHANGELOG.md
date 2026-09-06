# Changelog

**Version numbers.** This file is the internal BUILD history: the number counts every
change by its size and is not a stability promise. The PUBLIC package version lives in
`pyproject.toml` and `RELEASES.md` on its own 0.x track and moves only at deliberate
releases. Until 25.08.2026 both were one number, which is why this file runs from 0.1.1
to 6.x and why the public version restarted at 0.1.0.

## [6.23.0] — 2026-09-05
- feat(port): origin board stand of 2026-09-04 (origin Build 6.21.0) + English write edge
  - Ports the origin board from origin Build 6.19.15 to 6.21.0 (2026-08-26 → 2026-09-04
    afternoon): one-shot long-run launch (`▶ Start long run · up to 6h`), re-parenting
    items via `board_write.py --parent`, `/repo-file/` serving images, md viewer fixes (underscore emphasis, wrapped
    spans, front matter), top-row hover toolbar flip, sweep making undated done items
    archivable again, board-lint `dup_bodies`, retro-scan dating fixes.
  - Deliberately NOT included: the reply-suggestion experiment (origin Build 6.22.0,
    same evening) and `wall_ms` run logging (6.22.1) — untested by the owner; the port
    stops at the 04.09. afternoon commit, so no suggestion code exists in this tree.
  - `board_write.py --col` now takes the English column names `Now|Next|Backlog`
    (legacy internal keys still accepted); contract and skill texts follow.

## [6.22.0] — 2026-08-27
- feat(onboarding): board-look pages and a shipped update card
  - The introduction, the walkthrough and a new inspiration page are rebuilt in the
    board's own visual language and served same-origin from the package:
    `/welcome` (card 1), `/onboarding-showcase` ("Find your way around", keeping its
    path and the `#threads`/`#off-duty` anchors seeded cards link to) and
    `/inspiration`. All three are self-contained — no external asset, no request.
  - New optional starter card `13 · Get more from Superboard` opens `/inspiration`;
    the closer moves to `14 · Finish Getting started`. Card 4 now links the
    `#threads` section, and card 2 makes the workspace a git repository when it is
    not one yet (`git init` plus a first commit, decided rather than asked).
  - The Cockpit ships exactly one action, `superboard-update` — an agentic
    "Check for updates" card that snapshots the workspace in git, compares the
    installed package against PyPI, installs, verifies, merges local drift itself and
    closes with a short note on what is new. Its skill is seeded create-only to
    `.claude/skills/superboard-update/SKILL.md`, so a stranger's first click works.
  - Because one action ships, the Cockpit tab is present from the first start:
    card 7 now customizes an existing Cockpit instead of conjuring it. The
    zero-actions predicate stays for the workspace that deletes every card.

## [6.21.5] — 2026-08-26
- fix(release): allow GitHub merge noreply identity
  History leak scans accept GitHub's exact synthetic merge identity
  `GitHub <noreply@github.com>` while continuing to reject other github.com mail.

## [6.21.4] — 2026-08-26
- feat(onboarding): separate setup from real work
  - Fresh workspaces now seed an empty `My to-dos` topic beside the finite
    `Getting started` checklist, so ordinary work is visible without implying
    that every card is an agent job.
  - The first card now asks the agent to open the same-origin introduction and
    keep the card thread available for questions; the hard-coded tour buttons
    and opaque session-cut copy are gone.
  - Dedicated task-shaped cards explain threads/cache and help/settings, while
    the real-work step creates one approved manual card before offering an agent
    hand-off.
  - Normal cards receive their stable ID in the browser before their first save,
    and board saves are serialized, so consecutive additions cannot churn IDs or
    duplicate an ID-less local card during conflict recovery.
  - The release workflow can now build and smoke safely on a pull request or
    manual dispatch; PyPI publishing remains strictly tag-gated.

## [6.21.3] — 2026-08-26
- fix(onboarding): keep renamed tour card linked
  Card 4's clearer title initially stopped matching the UI's direct-guide rule.
  The static thread/cache CTA now follows the renamed card, with a regression test.
- fix(release): make the installed-wheel smoke import the installed wheel
  The smoke now launches from the fresh workspace and rejects any module path
  inside the checkout, preventing source-tree shadowing from producing a false pass.

## [6.21.2] — 2026-08-26
- fix(onboarding): make every starter card concrete
  - Replaced the generic optional-setup chooser with separate email digest,
    routine and later thread-learning cards.
  - Renamed the vague real-work hand-off to the literal outcome: add 3–8 current
    to-dos and run one; agent/model readiness is now its own verified step.
  - Removed Cockpit emphasis while retaining the deliberately highlighted Start
    card, and synchronized the tour, operating guide, architecture and tests.

## [6.21.1] — 2026-08-26
- fix(release): keep smoke compatible with Python 3.10
  The installed-wheel smoke now reads the public version without Python 3.11's
  `tomllib`, so the repository's declared Python 3.10 minimum is exercised honestly.

## [6.21.0] — 2026-08-26
- feat(port): board integrity guard, chat-card retirement, stale-tab save guard, CREW token count
  Port audit against the board this repo is projected from — the previous port pass had
  drifted; these are the genuine, tested gaps it found, not a full re-sync.
  - **Board integrity guard** (`board_integrity.py`, new): detects data loss board.md can't
    catch itself — a dead sidecar reference, a `@gc-parent` pointing at nothing, an orphaned
    thread file. Surfaces in the cockpit payload and, on a finding, a small header tile
    (silent otherwise — a permanent green 0 is a tile nobody reads after a week).
  - **Cockpit chat-card retirement** (`sweep.py`): daily Cockpit chat cards now retire
    themselves after 3h of inactivity instead of piling up unarchived.
  - **Stale-tab save guard** (`server.py`/`index.html`): a whole-board save can no longer
    silently drop an item the saving tab never loaded — the server 409s unless the client
    explicitly declares the deletion (`removedIds`), and the client's conflict-retry now
    adopts server-only items instead of losing them.
  - **CREW token count**: the finished-runs header now counts `cache_read`/`cache_creation`
    into `tok`, so a cheap warm-cache run with millions of cached tokens no longer looks
    like a run with no context.
  - **gc_runner**: a successful run with an unreadable usage block now still stamps
    `@gc-last` (as `~0k`) instead of leaving the item looking like it never ran.
  - **bump.py**: stopped auto-writing `pyproject.toml` on every commit — that's the public
    release number, moved only by a deliberate release, never by a build-stand bump.
  - **make-icon.py**: fixed a live break — `index.html` had already renamed its brand-mark
    constant, this script's regex hadn't followed.
  - Left open on purpose (see the origin repo's port ledger for why): the OpenCode runner
    adapter, the color-token-discipline ratchet (needs its own migration here first), and
    the waiting-rail UI (needs a visual pass).

## [6.20.3] — 2026-08-26
- fix(release): close the final public trust-surface gaps

- **Fresh files and protocol links are English.** New workspaces create `rituals.json`
  with an English key and emit `full text` / `full reply` sidecar pointers; existing
  `rituale.json` files and German pointers remain readable. Upgrade bootstrap copies
  legacy ritual content before it seeds the new filename, so nothing silently vanishes.
- **A started run stays visibly started.** The thread remains open with persistent
  progress and stop/inspect controls instead of disappearing after a successful click;
  it adopts a newly assigned card ID and refreshes the completed reply in place. The
  footer now says `active` so zero cannot be mistaken for a cumulative run count.
- **The release contract is executable.** Public version `0.1.0`, package URLs,
  SECURITY policy, release notes, release ritual, and a tag-gated Trusted Publishing
  workflow are present. Internal bumps cannot overwrite the public version; the CLI
  reports it explicitly. The workflow publishes the exact artifact it built and smoked,
  then creates the matching GitHub release.

## [6.20.2] — 2026-08-25
- fix(onboarding): rebuild the stranger-safe first run

- **The first two paid documentation rounds are gone.** One numbered Start card
  opens a desktop-width walkthrough directly, while a second static lesson explains
  manual to-dos, standing threads, context cuts and prompt cache without using a model.
- **Workspace setup now earns its place as step two.** It adapts to a blank home or
  code repository, creates only approved context/topics, and warns that moving to a
  parent workspace does not carry over worked threads or spend history.
- **Optional work is optional.** Email, routines, later thread-learning and extra
  agent platforms are created only after one applicability round. Cockpit setup moves
  into Now, states its longer expectation and announces the tab when it appears.
- **The auto-mode boundary is explicit.** README and first-start output explain that
  agent runs inherit host access and MCP/provider configuration, and point interactive
  work to exact terminal handoffs instead of pretending Superboard adds approvals.
- **Fresh files speak one vocabulary.** Bootstrap now writes `Now / Next / Backlog`
  and English section headings; old German headings remain readable internally.
- **Triage survives the failure seen in all stranger journeys.** A flatter response
  contract plus conservative bracket repair accepts the malformed reply, keeps the last
  raw failure for diagnosis, and prevents automatic retries inside a failed slot.

## [6.20.1] — 2026-08-25
- feat(onboarding): personalize off-duty view

- **Off Duty is personalized, not a hard-coded worldview.** Its setup card shows
  a fictional before/after, asks what this board is for, and saves approved exact
  hidden/visible topic sets. The toggle remains view-only, and unknown/new topics
  stay visible by default.

## [6.20.0] — 2026-08-25
- feat(onboarding): build capability-grounded first run

- **First run is concrete without becoming fragmented.** Welcome, visual tour,
  real-work import and workspace boundaries lead; email, Cockpit, night rest,
  one optional routine and thread learning stay separate. Claude Code, Codex and
  OpenCode receive one setup card each only when missing, and installation plus
  the platform's truthful login/provider connection remain one outcome.
- **A fresh workspace has no empty Cockpit.** The tab appears only after at least
  one valid action exists, carries the subtitle “your one-click actions,” and
  renders only populated action zones. Existing `actions.json` entries are never
  rewritten by onboarding.
- **Cockpit setup is resumable and capability-grounded.** The workspace client’s
  new `--ensure-card` verb creates the extension follow-up exactly once. The setup
  mission verifies skills, MCP names and CLIs without reading secrets or starting
  logins, then requests approval before a surgical `actions.json` edit.
- **The visual tour is real, local and safe to publish.** A packaged same-origin
  page explains board/thread/decision-sheet interaction and shows fictional
  maintenance, knowledge and personal-sports Cockpits. It contains no maintainer,
  user or work data, and onboarding derives its URL from the running board origin
  instead of assuming the default port.
## [6.19.0] — 2026-08-24

The first five minutes of a fresh install were not true. Three verified breaks,
all on the path a newcomer actually walks.

- **Night-rest overlays are inactive by default.** The footer pill, reminder
  ladder and mandatory 23:00–06:00 pause require a literal
  `night_pause.enabled: true` in the workspace's `board.config.json`; the setting
  applies after restart.
- **Late-night rendering no longer aborts the UI during script startup.** The
  shared DOM helper and view filters are initialized before render-capable clock
  callbacks, and clock ticks are isolated so a failure cannot leave navigation
  half-wired.
- **The workspace board client follows the server it belongs to.** Agent runs now
  receive the active URL, so `--docs`, card and topic commands work on supported
  non-default ports; redirecting runtime data with `GC_DATA` no longer moves the
  advertised `.superboard/board_write.py` path.

- **The very first card read files that were not there.** `Start here` told the
  agent to answer from "this workspace's own README.md" and to point at
  `ARCHITEKTUR.md` for depth. A fresh workspace receives neither. Copying them in
  would only move the problem — a copied doc goes stale on the next upgrade — so
  the docs are now SERVED by the running version at
  `GET /api/docs/{readme,architecture,changelog}`, and the card reads them with
  `python3 .superboard/board_write.py --docs readme`. If that fails, the card now
  tells the agent to say so rather than improvise.

- **Agents were told "never edit board.md" while holding no tool that reliably
  worked.** Setup cards ask the agent to create topics and to-dos; the bundled
  skill named no write mechanism at all, and the run prompt named
  `python3 -m superboard.board_write`, which assumes the agent's shell can import
  a package installed in the server's (possibly ephemeral `uvx`) environment. An
  agent in that position does the helpful thing and edits the file by hand — which
  breaks the single-writer invariant, and every later run reads the thread and
  learns that hand edits are normal. `board_write.py` is now pure standard library,
  imports nothing from the package, is copied into every workspace at
  `.superboard/board_write.py` (refreshed on each start, so an upgrade never leaves
  an agent on an old client), and covers the whole write surface: `--show`,
  `--body-file` + `--body-etag`, `--stage`, `--new-card`, `--new-topic`, `--docs`.
  `--new-card` never starts a run: creating work on the user's behalf must not also
  spend the user's tokens. The bundled skill and the run contract both name that
  path now.

- **"Claude Code is not installed" was printed to the terminal only.** A newcomer
  who never installed it met a ▶ Agent button that silently did nothing. The check
  has one implementation (`server.runner_status`) behind
  `GET /api/runner-status`; the board shows the reason on the first screen and
  marks the buttons `▶ Agent (not installed)` / `(not signed in)` with install
  instructions on hover.

Also: the first real hand-off moved from step four to step two. `Bring in your
real work` now sits directly behind `Start here` — shaping paths, topics and run
profiles before any real work exists asks people to configure a tool they have not
used yet. The checklist stays optional and visibly finite at eight cards.

## [6.18.16] — 2026-08-24

Release candidate: the onboarding work, the launch assets, the macOS smoke test
and the pending board changes are one codebase again, and three things that
should not ship were fixed first.

- **A page in your browser could start agent runs on your machine.** Binding to
  `127.0.0.1` kept the server off the network but not away from the browser, and
  a browser is a local program: any site you visited could POST to the board.
  `Content-Type: text/plain` is a CORS simple request, so no preflight fired and
  the `Origin` was never examined — a foreign page could create a card and start
  a run, which is code execution, not just a write. Every state-changing request
  now passes a guard first: `Sec-Fetch-Site: cross-site` is refused, an `Origin`
  that is not this server on this port is refused, and a request with a body must
  declare `application/json`. Requests without an `Origin` still work — that is
  what non-browser tooling looks like — and reads are unaffected.
- **Mis-indented lines are no longer thrown away.** A body line needs two leading
  spaces; one space or a tab fell through every branch and vanished on the next
  save. The lost guards compare known line families, and free text is not one, so
  nothing complained while `board_lint.py` could see the loss. The parser keeps
  such lines now, in all three places that parse an item.
- **`board_lint.py` reports duplicate ids and duplicate topic headings.** A bad
  edit that splices a section twice is now visible instead of quietly doubling a
  board.
- **The installed-wheel smoke test can no longer pass on a stale wheel.**
  `dist/` is ignored and survives between sessions, and Superboard's version
  travels with the code it is projected from — so several commits legitimately
  carry the same number and the version string could never prove which commit a
  wheel came from. The build clears `dist/`, refuses more than one wheel, and
  stamps the commit into `dist/BUILD_SHA`; the assertions refuse a stamp that
  disagrees with the checkout. `scripts/smoke-local.sh` is the local path the
  docs promised and nothing implemented.
- **A forced five-minute prompt-cache bucket is no longer set.** Measured over 357
  resumed runs, only 7 % started warm — the board's own cadence is far longer than
  five minutes, so the cache was written and almost never read. The variable is now
  actively removed from the run environment (a run started from inside a run would
  otherwise inherit it), and the CLI picks its own bucket. The card's recency pill
  is unchanged.
- **`board_write.py` ships, so the agent contract is true.** The contract tells
  agents to write item bodies through the API rather than editing `board.md`;
  `python3 -m superboard.board_write` is that path, with a revision token and a
  409 on a stale one. `guard_hook.py` is the optional Claude Code companion that
  re-lints after every write.
- **The macOS smoke test now actually exercises the product.** It was written
  against an older base and never survived the onboarding change: it started the
  server without a workspace path, which a first start refuses by design, so the
  server never came up and every assertion below it was unreachable. It also still
  looked for a welcome card that the onboarding journey replaced. It now passes the
  workspace explicitly, as a user types it, and asserts the Getting started row and
  the Start here card — the two things screen one actually promises.
- **Documented rather than pretended away:** `docs/USING-SUPERBOARD.md` now has a
  known-limitations section for the two places where hand-editing `board.md` can
  cost an item its identity, and says what to do instead.

## [6.13.2] — 2026-08-23
- feat(onboarding): recommend workspace capabilities

  Shape this workspace now inventories existing skills, tools, and MCPs and
  recommends a small baseline tied to the user's work. Browser automation is the
  default recommendation for web/product work so agents can operate and verify
  rendered interfaces, not merely inspect source.

## [6.13.1] — 2026-08-23
- fix(onboarding): make email setup a concrete digest

  The Soon card now builds and tests an email digest instead of discussing an
  abstract mail workflow. Its prepared turn focuses on the desired signal,
  cadence, real sample, and repeatable action; redundant credential and approval
  warnings were removed from the starter copy.

## [6.13.0] — 2026-08-23
- feat(onboarding): add superskills setup journey

  A fresh workspace now gets eight small, checkable setup missions: workspace
  foundation and email are concrete, optional skills come from a separate
  workspace-owned catalogue, and a final Parked card archives the completed
  Getting started topic without discarding its history.

## [6.12.7] — 2026-08-23
- fix(onboarding): highlight Start here instead of Shape this workspace

  The bold/bordered highlight — the board's own "one card stands out" convention
  — sat on the second setup card by leftover default. Owner call: if one card
  gets the visual lead, it should be the 60-second first step, not the second
  one. The other six cards are unchanged.

## [6.12.6] — 2026-08-21
- feat(onboarding): make instance files workspace-owned

  First-run bootstrap now creates `actions.json`, `rituale.json`,
  `board.config.json`, and a Superboard administration skill in the user's
  workspace with create-only semantics. Server registries, configuration, and an
  optional instance contract resolve there instead of inside the installed
  package, so an agent can genuinely personalize a wheel-installed board and an
  upgrade cannot clobber that work.

- feat(onboarding): replace sample content with one explicit setup mission

  A fresh workspace now contains one neutral topic, one pending `Set up my board`
  thread, and no generic actions or rituals. A browser with no saved tab preference
  lands on To-dos for that first render, while startup remains token-free and later
  visits continue to honor the user's own tab choice.

- feat(onboarding): make setup a checklist of separate to-dos

  The single `Set up my board` mission becomes six ordinary to-dos — topics, paths,
  cockpit actions, first real hand-off in Now; one optional connection and a
  look-back-after-a-few-days review in Soon. Each has its own id, its own thread and
  its own pending turn, so a first-time user starts one step, sees a result, ticks it
  off, and can skip the rest. Owner direction: onboarding should be work you check
  off, not a wizard you sit through.

- feat(onboarding): make the first-run boundary explicit

  A new installation now names its high-level workspace in the start command and
  gets a non-fatal preflight for repository placement and Claude readiness before
  any files are created. The first run profile is the authenticated Claude CLI's
  own default instead of a hard-coded Opus choice. The six setup cards now follow
  the real journey: shape the workspace, choose the agent mode, bring in real work,
  build one useful button, optionally connect a tool, then learn from day-three
  evidence. A blank workspace may grow a tiny neutral context scaffold, but only
  after showing the user the proposal.

- feat(board): complete a to-do from its card overlay

  A right-aligned `✓ Done` action now completes the open to-do without returning to
  the matrix first. It reuses the existing checkbox path, closes the overlay, and
  keeps the just-completed card visible until reload so the checkbox remains an
  immediate undo.

- feat(onboarding): a 60-second first to-do before the real setup starts

  A new "Start here" card now leads the checklist (five in Now, still two in Soon).
  Its only pending turn is one queued question — a very short overview of how this
  board works — answered from the workspace's own README, so the first thing a new
  user does is open a card, press `▶ Agent`, read a short reply, and tick it off in
  under a minute, before any of the heavier setup cards ask for real decisions.

- fix(ui): the item overlay's primary action is now visually correct

  `▶ Agent` — the button that actually starts a run — carries the filled accent
  color that used to sit on `Send`, which only appends a thread turn and never runs
  anything. `Send` is renamed `Save` and restyled as the secondary action, with a
  tooltip spelling out that it does not dispatch to the agent; `▶ Agent`'s tooltip
  now says it saves the message too. Matching hint text, keyboard-shortcut labels,
  and decision-sheet copy elsewhere in the overlay were adjusted for the same
  distinction.

## [6.12.5] — 2026-08-21
- feat: catch up with the board this package is projected from

  First full port pass since the extraction. Twenty files had drifted; the version
  number jumps from 6.8.1 to 6.12.5 because it is the *board's* number, not a
  separate public track.

  What arrived, grouped:
  - **Board file boundary is bilingual.** `parse` accepts German and English column
    and section headings forever, `serialize` always writes English — an existing
    board migrates itself on the next save, no migration step.
  - **Multi-agent run profile** `opus-multi`: the `Workflow` tool stays disabled by
    default (its schema costs ~8k tokens per turn) and is opt-in for a single item.
  - **Codex cache window** is now measured instead of guessed: a live 60-minute
    countdown plus a counter for how many foreign runs are eating the window.
  - **Acknowledge instead of run** (`✓`): an action card whose round is finished but
    whose thread is still open can be checked off, with a per-zone counter.
  - **Restart drain guard:** while a restart waits for running agents to finish, no
    new run starts. (The restart *trigger* stays out — an installed package restarts
    through its own process, not through a script in a repo.)
  - **`radar_watch`** is new: it lets the dev radar report only what actually moved
    since the last sweep, and has a small agent judge each finding in the item thread.
  - Fixes: a guaranteed `NameError` on the quick-capture path, repo links opening
    inconsistently, an unvalidated session id reaching the clipboard, the retro scan
    trusting a neighbouring sidecar as evidence, and a lint false positive on
    round-tripped headings.
  - `bump.py` no longer counts instance content (`actions.json`, `rituale.json`,
    `board.config.json`), and `major` is never automatic any more.

  Deliberately not ported: everything that serves a private integration (product
  metrics, SSO, personal tooling) and the categories in `index.html`, which stay an
  empty `INSTANZ-CONFIG` block — grouping belongs to whoever runs the board.

## [6.8.1] — 2026-08-18
- chore(version): adopt the upstream board version instead of a separate 0.x track

  `pyproject.toml` and `server.py` now both read 6.8.1, written by the origin
  instance's port tooling. Two numbers for the same code were exactly the drift
  the port ledger exists to prevent — an installed Superboard should say which
  board build it contains.

## [0.1.1] — 2026-08-18
- fix(server): fail readably on a taken port and bind before starting the journal watch

  Starting a second board on an occupied port raised a bare `OSError: [Errno 48]`
  traceback; it now exits with one line naming the port and the way out. The bind
  also moved ahead of the journal watch: that watch talks to `127.0.0.1:<port>`,
  so on an occupied port the losing process used to post its orphaned runs into
  the board that already owned the port.

## [0.1.0] — 2026-08-18

Initial standalone extraction: Markdown-backed board, persistent agent threads,
local runner, cockpit frame, fictional sandbox workspace and installable package.
Private instance integrations and content are deliberately excluded.
Scheduled agent work and MCP credential forwarding are disabled by default.
