# Releases

Public version history of Superboard. The public number (`pyproject.toml`, what PyPI
and `uvx` show) moves only at deliberate releases and follows the usual reading:
patch = fixes, minor = something new, major = something you should read about
before upgrading. `Build` names the internal board stand that ships inside a release
— that number counts every change by size and is not a stability promise.
`CHANGELOG.md` inside the package is that internal build history.

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
