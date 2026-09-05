# Releases

Public version history of Superboard. The public number (`pyproject.toml`, what PyPI
and `uvx` show) moves only at deliberate releases and follows the usual reading:
patch = fixes, minor = something new, major = something you should read about
before upgrading. `Build` names the internal board stand that ships inside a release
— that number counts every change by size and is not a stability promise.
`CHANGELOG.md` inside the package is that internal build history.

## [0.3.0] — 2026-09-05 · the write edge, documented — and the board stand of September 4

- README now says in its first lines who Superboard is for (people already running
  several agents, review load as the bottleneck) and who it is not for, shows what
  `inbox/board.md` actually looks like, and documents the **write edge**: any script,
  cron job, CI step or other agent can post into a card through
  `.superboard/board_write.py` or `POST /api/gc-append`. You do not have to switch
  your workflow to use the board as the thread that outlives the terminal.
- `board_write.py --col` speaks English: `Now`, `Next`, `Backlog`.
- **▶ Start long run · up to 6h** in the card menu: a one-shot exception for a run
  that needs hours, visibly marked on card, overlay and CREW, never a sticky toggle.
- Re-parent a card under another (one level) from the client: `--parent <gc-id>`.
- Smaller: repo HTML can reference screenshots relatively (`/repo-file/`), the md
  viewer renders `_emphasis_`, wrapped spans and YAML front matter, the hover toolbar
  of top-row cards no longer flips into the header, done items without any timestamp
  archive again, board-lint reports content-identical duplicate cards.
- Not in this release, on purpose: the reply-suggestion experiment the origin board
  gained on the same evening. It ships when its owner has used it. Build 6.23.0
  (origin stand 2026-09-04, origin Build 6.21.0).

## [0.2.0] — 2026-08-27 · guided first run and a self-updating Cockpit

- Three onboarding pages in the board's own look, served from the package: a
  Welcome walkthrough (`/welcome`), "Find your way around" (`/onboarding-showcase`,
  threads, cache, Off Duty, Cockpit) and "Get more from Superboard" (`/inspiration`,
  what the Cockpit is plus four things to simply ask for) — fourteen concrete
  Getting-started cards now, the last one optional.
- The Cockpit ships with one button from the first start: **⬆ Check for updates**
  runs an agent that snapshots the workspace in git, compares the installed package
  with PyPI, installs, verifies the board still starts, merges your local changes
  itself and ends with a short note on what is new. Its skill is seeded into the
  workspace so the first click works offline of any catalogue.
- Setting up the workspace now makes it a git repository when it is not one yet.
- README leads with a 16-second story loop of a task going through the board.
- Fixed: a fresh install landed on the Cockpit tab instead of the to-do list once a
  Cockpit action ships. Build 6.22.0.

## [0.1.0] — 2026-08-26 · first public alpha

- A local Now / Next / Backlog board where an ordinary to-do can stay manual or
  become a standing conversation with an agent.
- Thirteen concrete onboarding cards sit separately from an empty My to-dos area;
  the agent-led introduction, first genuine task, runner setup, help, Cockpit
  actions, and optional routines each have one clear outcome.
- Claude Code is the supported default runner; Codex on macOS is experimental.
- Plain Markdown and JSON remain the source of truth—no hosted account, database,
  or bundled model access.
- Release gates cover the exact wheel, macOS and Ubuntu smoke tests, a real-agent
  confirmation journey, and private-vocabulary leak scans.
