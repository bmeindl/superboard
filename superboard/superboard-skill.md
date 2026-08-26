---
name: superboard
description: Set up and maintain this Superboard workspace: board topics, cockpit actions, rituals, paths, and safe agent operating rules.
---

# Superboard workspace administration

Use this skill when the user asks to set up, configure, reorganize, or maintain
their Superboard.

## Ownership boundary

The Python package is read-only product machinery. Never edit files in the
installed package or an `uvx` cache. The current working directory is the user's
workspace and owns the mutable state:

- `inbox/board.md` — topics, columns, to-dos, bodies, and standing threads
- `actions.json` — one-click cockpit jobs
- `rituals.json` — optional daily or weekly prompts
- `board.config.json` — owner and local identity labels
- `.claude/skills/` — reusable procedures specific to this workspace
- `.superboard/` — runtime data plus the board client `board_write.py` (product
  mechanics, refreshed on every start); do not treat it as user configuration

Package upgrades must never overwrite these files.

## Writing to the board

The **server is the only writer** of `inbox/board.md`. Never edit that file, and
never hand-write its markdown. Every board change goes through the client that
this workspace carries at `.superboard/board_write.py` — plain standard library,
so any `python3` can run it, and it talks to the local board server over HTTP:

```
python3 .superboard/board_write.py --help
python3 .superboard/board_write.py --id <gc-id> --show
python3 .superboard/board_write.py --id <gc-id> --body-file <path> --body-etag <rev>
python3 .superboard/board_write.py --id <gc-id> --stage '<stage> · <note> *(YYYY-MM-DD)*'
python3 .superboard/board_write.py --new-card '<title>' --topic '<topic>' --col Jetzt
python3 .superboard/board_write.py --ensure-card '<title>' --topic '<topic>' --col Jetzt
python3 .superboard/board_write.py --new-topic '<name>'
python3 .superboard/board_write.py --docs readme|architecture|changelog
```

`--new-card` deliberately never starts an agent run: creating work on the user's
behalf must not also spend the user's tokens. The user presses ▶ Agent.
`--ensure-card` has the same behavior but first checks for an active card with
that exact title, so an interrupted setup can resume without duplicating work.

`--docs` reads the running version's own documentation. Superboard does not copy
its README into your workspace, because a copied doc goes stale on the next
upgrade — ask the server instead.

If the client fails, say so and stop. A hand-edit that "works" is worse than a
visible error: it breaks the single-writer invariant, and every later run reads
the thread and learns that direct edits are normal.

## Product vocabulary

- A **to-do** is a card with a persistent agent thread.
- A **topic** is a board row such as Development, Clients, or Personal.
- An **action zone** is one of the fixed cockpit purposes: intake, deliver,
  self-improvement, maintenance, or personal.
- An **action** is a repeatable one-click agent job in `actions.json`.

Do not call both topics and action zones “categories.”

## Safe changes

Inspect the existing file before changing it and preserve unknown fields and
all `@gc-*` thread markers. Prefer the smallest edit that expresses the user's
choice. Never replace an existing workspace file with a starter template.

An action needs a lowercase slug `key`, a user-facing `label`, one of the action
zone names in `group`, and a self-contained `prompt`. State any external write,
message, purchase, publication, deployment, or authentication boundary inside
that prompt. Do not store tokens, passwords, cookies, or secret file contents in
an action, ritual, board card, or thread.

Before inspecting outside the active workspace, explain what you want to inspect
and why. A workspace rooted inside one code repository may be too narrow for a
high-level personal task host; recommend a parent workspace when appropriate,
but never move files or change the root without the user's approval.

## A blank workspace

When the user asks to shape an otherwise blank workspace, preview a minimal
neutral scaffold before writing it:

- `CLAUDE.md` — who the owner is, how the agent should work, the confirmed path
  map, and the instruction to read the context index on startup
- `context/README.md` — an initially small index of durable knowledge
- `inbox/scratch.md` — loose observations and learnings waiting to be sorted
- `.gitignore` — runtime data, temporary files, and local secrets

Build the content from the user's answers; never copy a product manager, software
developer, employer, or Superboard maintainer's structure into it. Keep the first
version small. Do not initialize git, create a remote, install a skill, or connect
an account unless the user approves that separate effect explicitly.

Superboard includes only this administration skill. Workspace hygiene and
learning/dreaming are useful later, but they must be installed as public-safe,
workspace-neutral skills rather than copied from another person's workspace.
The optional catalogue convention is `superskills/skills/<slug>/SKILL.md` inside
the active workspace. A setup card may read a named catalogue skill when it exists,
but absence is a normal offline state. Preview the local adaptation and ask before
copying it into `.claude/skills/<slug>/`; never auto-install or auto-update it.

The administration skill can support a native **Board cleanup** action: inspect
active cards, archive or close only work the user has approved as finished, and
flag stale or ambiguous cards for review. It is not a general filesystem cleanup.
Health checks, workspace hygiene, learning, and system review are separate
capabilities and may be proposed only when their actual procedure is installed
and ready in this workspace.

After changing actions, rituals, configuration, or product code, tell the user
whether the running board needs a restart. Prove user-visible behavior in the
running board before calling the change done.
