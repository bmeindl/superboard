# Superboard

**A local board for work you delegate to coding agents.** Drop in rough work;
the agent researches, builds, or returns the decision only you can make. Every
card keeps its own persistent thread, and the underlying state stays in plain
files you own.

[![leak scan](https://github.com/bmeindl/superboard/actions/workflows/leak-scan.yml/badge.svg?branch=main)](https://github.com/bmeindl/superboard/actions/workflows/leak-scan.yml)

![A fresh Superboard workspace with twelve concrete onboarding cards](https://raw.githubusercontent.com/bmeindl/superboard/v0.1.0/docs/assets/superboard-hero.png)

```sh
uvx superboard ~/Superboard
# then open http://localhost:47822
```

## The one-minute version

1. Open the board. A fresh workspace shows one **Getting started** category.
2. Open **1 · Start here · Tour Superboard**. Its desktop walkthrough is static:
   no agent run, login, or model tokens.
3. Use **2 · Set up this workspace**, then **3 · Add your first real to-dos**.
   The agent adapts the foundation to a blank home or an existing repository.
4. The remaining cards each name one outcome—agent/model setup, Cockpit, email
   digest, one routine, Off Duty, night rest or later thread learning—so you can
   complete or skip them independently.
5. Add more work whenever. `Enter` creates a card; `Cmd/Ctrl+Enter` creates it and
   starts the agent.
6. Finish onboarding. The final card archives the setup threads and removes the
   Getting started category.

This is not a five-minute setup, and it does not pretend to be. A board becomes
genuinely useful over weeks, as your own threads accumulate.

There is no settings maze. Topics, actions, rituals, context, and skills are
workspace files. Ask the agent to change them; inspect the diff whenever you
want. Optional procedural skills can be copied from a separate catalogue one at
a time, previewed first, and then customized locally.

## What you need

- Python 3.10+ and `uv`/`uvx`. The release gates cover macOS and Ubuntu; Windows
  has not been verified and is not supported in this alpha.
- Claude Code installed and authenticated for the supported default runner.
- Codex is an experimental macOS runner and uses the CLI bundled with ChatGPT.
  OpenCode is not a supported runner in `0.1.0`.
- Provider usage: Superboard does not include model access or tokens.

The board still opens without an agent CLI, but hand-offs cannot run — and it
says so: the first screen carries the reason and the ▶ Agent buttons are marked
rather than silently inert.

## What an agent run can do

`▶ Agent` starts the selected CLI in auto mode. The run inherits that CLI's host
access and configured MCP/provider setup; when the task requires it, the agent can
edit or commit inside the workspace and may propose machine-wide or outside-workspace
actions. Superboard does not wrap the CLI in a second approval system. Tell the agent
what is off-limits, ask it to change its local operating rules, or ask for exact
terminal handoff commands when an interactive step is needed. The files and git diff
remain the review surface.

## Why local files?

Superboard has no cloud account, database, or sync service. The board, context,
and learned working rules stay inspectable and portable inside your workspace.
Changing agent providers does not mean abandoning what the workspace learned.

Each `▶ Agent` starts a fresh CLI process. Continuity comes from a resumable
provider session plus the durable board thread — not from a hidden Superboard
memory. A warm provider cache may reduce repeated tokens; a cold cache never
loses work.

## How it works

Superboard is the piece that holds your work and coordinates the agents doing it — a personal task host. The **board** is the visible surface — columns, cards, one glance. Each **card** is a task with its own standing thread: the full conversation between you and the agent working it, persistent across weeks. A **runner** executes — it picks up cards you've handed off, works headlessly, and reports back into the thread: results, or a short decision sheet when only you can decide. Underneath: plain local markdown files. No database, no account, no sync. The agent brings the intelligence; the files keep it honest.

## The first weeks

Superboard doesn't promise one-minute setup. It promises an honest onboarding — the kind you'd give a strong new hire. Week one, it asks too much: it doesn't know your projects, your people, or which decisions are yours alone. You correct it constantly, and the correcting is the investment — every correction lands in plain files it reads next time, and you can open any of them to see exactly what it thinks it knows. A few weeks in, the questions change character: less "what is this?", more "you killed a similar idea in March because it competed for your attention — still true?" And it compounds with what you already have: Superboard runs on top of your existing agent setup, and the more you bring — skills, context, working habits — the faster it gets good. Starting from zero works too; it just makes the first weeks matter more. Tools that promise instant magic tend to plateau fast. Superboard starts slower — and keeps compounding.

## Agentic first — it grows, and you grow it

The mechanics work on day one: board, runner, standing threads, decision sheets, a working skill set — extracted from a system used daily for months. The personalization is what takes weeks. And nothing about it is finished, by design: there is no feature backlog between you and the tool — when you want the cards to work differently, you don't file a request, you tell your agent to rebuild them. The whole system is plain files and readable code, small enough for an agent to navigate and change, with every change reviewable. From the first week it grows toward you: your skills, your rules, what it has learned about how you decide. Every install grows toward its owner — that divergence is the point, not a side effect. And it flows both ways: when your setup grows something good — a skill, a routine, a sharper way of asking — it's built to flow back through ordinary open-source contribution and become part of everyone's next start. That's open source applied to a tool whose job is to learn.

## Where this goes

Today, Superboard is a board. The direction is a working morning that starts with three prepared items instead of forty open loops — everything else researched, built, filed, or consciously not started while you were away, each weighed in the open against priorities you set. Questions that get sharper the longer you work together. And because everything it learns lives in inspectable files, changing models doesn't have to mean starting over. That's the target narrative, told honestly as direction — the full version is in [PITCH.md](https://github.com/bmeindl/superboard/blob/v0.1.0/PITCH.md). This board is step one of exactly it.

## Start here, then ask the agent

The board and its onboarding cards are the primary product documentation. The
README deliberately stops at orientation; users should not need to study a
manual before doing useful work.

- [Using Superboard](https://github.com/bmeindl/superboard/blob/v0.1.0/docs/USING-SUPERBOARD.md) — installation, workspace files,
  onboarding behavior, customization, and restart rules.
- [Development and test rigs](https://github.com/bmeindl/superboard/blob/v0.1.0/docs/DEVELOPMENT.md) — sandbox, fresh-wheel test,
  and privacy gates.
- [Architecture](https://github.com/bmeindl/superboard/blob/v0.1.0/superboard/ARCHITEKTUR.md) — contracts and trust boundaries for
  agents and contributors.
- [Product direction](https://github.com/bmeindl/superboard/blob/v0.1.0/PITCH.md) · [Support posture](https://github.com/bmeindl/superboard/blob/v0.1.0/SUPPORT.md)

Superboard is alpha-stage personal tooling, not a hosted multi-user project
manager or a supported service. The point is a small, understandable frame that
your own agent and workspace can grow into.

## License

MIT — see [LICENSE](https://github.com/bmeindl/superboard/blob/v0.1.0/LICENSE).
