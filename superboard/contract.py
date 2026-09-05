"""Compose the agent contract from generic mechanics and instance-specific rules.

The runner needs two variants of the same contract: the full version for fresh
sessions (and once after a board compact), and the short reminder for normal resume
turns. The order remains deliberately explicit: instance rules are inserted between
core rules so that extracting them does not change the resulting prompt.

Workspace `board.contract.md` contains optional instance knowledge and is not created by
the generic bootstrap. When it is absent, this module renders only the safe core. A
malformed existing file, however, is a startup error rather than a silent safety downgrade.
"""

from __future__ import annotations

import re
from pathlib import Path

import config as _cfg
import paths as _p

INSTANCE_CONTRACT_PATH = _p.CONTRACT

_SECTION_RE = re.compile(
    r"(?ms)^<!-- contract:(full|reminder)\.([a-z0-9_-]+) -->\n"
    r"(.*?)\n<!-- /contract -->$"
)


def _instance_rules(path: Path) -> dict[str, str]:
    """Load marked rule blocks. A trailing `\\` only wraps source text:
    it and its newline disappear during rendering, as in Python string literals."""
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"Cannot read instance contract: {path}: {exc}") from exc

    rules: dict[str, str] = {}
    for match in _SECTION_RE.finditer(source):
        key = f"{match.group(1)}.{match.group(2)}"
        if key in rules:
            raise ValueError(f"Duplicate contract block: {key}")
        rules[key] = match.group(3).replace("\\\n", "")

    starts = source.count("<!-- contract:")
    ends = source.count("<!-- /contract -->")
    if starts != ends or len(rules) != starts:
        raise ValueError(f"Invalid contract markers in {path}")
    return rules


def _core_rules(owner: str) -> dict[str, str]:
    """Protocol, safety, and state handoff — works without the origin instance."""
    return {
        "full.final_message": f"""\
- Your VERY LAST message is written verbatim to the board thread as an @gc-re: reply — \
automatically, so you do not need to read or edit board.md for this (touch it only if the task \
requires changing or moving the item itself). Write a compact, readable answer to {owner} \
(in English, result first, no meta-commentary).""",
        "full.short_line": f"""\
- First line of the final message = a self-contained one-sentence summary (the result, ≤200 \
characters), then a blank line, then the details. The thread shows only this first line — {owner} \
can expand the full version when needed. If the reply's core is a QUESTION to {owner} (you need \
his input and there is no decision sheet for it), start the first line with `❓ ` — the board \
uses this marker to flag the item/card as "needs input" (decision sheets and 🔑 handoffs are \
detected automatically, only the plain-text question needs the marker).""",
        "full.safe_work": """\
- You may work as in a normal session (research, read/edit files, build). But ask one question too \
many rather than carrying out something risky headlessly — if something is too large or unclear, \
complete the safe portion and ask the follow-up question in the final message.""",
        "full.todo": """\
- Strongly recommended for multi-step work: keep your own sub-step list (task-list tool if \
available, otherwise a short numbered plan in your output) and update it as you go.""",
        "full.auth": """\
- Authentication boundary (interactive login required: AWS SSO device authentication, Okta, \
browser login, etc.): DO NOT fight it or run into the timeout — complete everything that works \
without authentication, then write a handoff turn: the first line of the final message starts with \
`🔑 CLI-Handoff nötig: <what is missing>`, and the details contain a ```sh block with the \
ready-to-run commands (the board shows a ⧉ copy button for it). There are two blocker classes: \
AMBIENT (credentials work across processes, e.g. `aws sso login --profile …` → ~/.aws-Cache): \
provide only the login command and say that ▶ Agent / `@gc: continue` is sufficient afterwards \
— the next run starts headlessly with fresh credentials. SESSION-BOUND (can be resolved only in \
the running session): provide the two lines `claude --resume <SESSION>` + `!<auth-cmd>` — write \
`<SESSION>` literally; when copied, the board UI replaces it with your actual session UUID, and \
the owner then takes over the session interactively. Local secret-file credentials that the runner \
itself may not read are SESSION-BOUND too: put the shell's `source <credential-file>` before the \
resume command so the resumed agent inherits the variables and runs the target workload itself; \
do not ask the owner to execute the workload merely to bridge the environment. When in doubt, \
treat it as ambient.""",
        "full.sidecar": """\
- Thread turns ending in `→ full text: …` or `→ full reply: …` are externalized long \
texts (the full text is at the referenced path under inbox/gc-threads/). The short line is only a \
pointer — read the file if you need details from an earlier turn. Your CURRENT task has already \
been fully expanded, so you do not need to load anything else.""",
        "full.board_client": """\
- The board is written by the SERVER alone. Never edit `inbox/board.md`, and never hand-write its markdown — use the board client that sits in this workspace at `.superboard/board_write.py` (plain stdlib, run it with any `python3`). It covers the whole sanctioned write surface: `--id <gc-id> --show` (read a body + its revision), `--id <gc-id> --body-file <path> --body-etag <rev>` (replace THIS item's body), `--id <gc-id> --stage '<stage> · <note> *(date)*'`, `--new-card '<title>' [--topic <topic>] [--col Now|Next|Backlog] [--card-body-file <path>] [--ask '<first turn>']` (create a to-do; it never starts a run — the user presses ▶ Agent), `--new-topic '<name>'` (add a board row), and `--docs readme|architecture|changelog` (read the product's own documentation from the running version). Run it with `--help` if unsure. If it fails, report the failure — do not fall back to editing the file.""",
        "full.docs": """\
- Documentation contract: IF this workspace keeps its own README.md / ARCHITEKTUR.md / CHANGELOG.md, a code change with user-visible or architectural impact updates the relevant one in the same pass (README = usage and setup, ARCHITEKTUR = invariants and trust boundaries — temporary facts and one-off context rot there, CHANGELOG = version history). Do not assume those files exist; check first. For Superboard's OWN documentation use `python3 .superboard/board_write.py --docs readme|architecture`.""",
        "full.git": """\
- Git: the native git status/instructions block is disabled for board runs (prompt-cache stability \
across resumes, see below) — reconstruct the standard trailer yourself on any commit you make: \
`Co-Authored-By: Claude <noreply@anthropic.com>`.""",
        "full.working_state": f"""\
- Maintain the working state — but only when there is substance. Did this run produce anything \
that appears neither in your thread reply nor in a commit (which files mattered, rejected options, \
a test result)? Then update the `### Working state` block in the item BODY after the `···` line, \
using the item-specific `board_write.py` command provided below — never edit `board.md` directly. \
SNAPSHOT, not a log: REPLACE the existing block, remove obsolete material, use at most ~12 lines \
and ~8 paths, and prefer paths over copied content. Suggested lines: `Goal:` · `Status:` (verified \
vs. merely claimed) · `Key files:` · `Decided:` (including rejected options and why — the only \
part that otherwise exists nowhere) · `Learned:` (OPTIONAL, only non-obvious findings that exist \
in no file) · `Open/Next step:` · `Branch/uncommitted:`. These lines are an OFFER, not required \
fields — omit anything that adds no value instead of filling it with padding. Three honest lines \
beat seven perfunctory ones. If the run was pure question-and-answer ping-pong, do not touch it.""",
        "reminder.final_message": f"""\
- Your VERY LAST message is written to the thread automatically as @gc-re: — do not read or edit \
board.md for this. First line = a self-contained one-sentence summary (≤200 characters), then a \
blank line, then the details. Reply whose core is a question to {owner} without a decision sheet \
→ first line starts with `❓ `.""",
        "reminder.board_client": """\
- Board writes still go through `.superboard/board_write.py` only (`--show`, `--body-file` + `--body-etag`, `--stage`, `--new-card`, `--new-topic`, `--docs`). Never edit `inbox/board.md`.""",
        "reminder.docs": """\
- Docs still apply: README.md (usage), ARCHITEKTUR.md (invariants, trust boundaries — not \
temporary facts), CHANGELOG.md. Update the relevant one if this run is a code change.""",
        "reminder.git": """\
- Git: still no native block (see the full-session note); still add the trailer yourself: \
`Co-Authored-By: Claude <noreply@anthropic.com>`.""",
        "reminder.working_state": """\
- If this run produced substantive material that appears neither in your answer nor in a commit \
(key files, rejected options, test result), REPLACE the `### Working state` block through the \
item-specific `board_write.py` command below — never edit `board.md` directly \
(snapshot, max ~12 lines). Do not touch it for pure ping-pong.""",
        "reminder.todo": """\
- Multi-step work → keep a sub-step list (task-list tool or short numbered plan); update it \
as you go.""",
    }


_FULL_ORDER = (
    "full.final_message",
    "full.short_line",
    "full.item_body",
    "full.reply_style",
    "full.board_client",
    "full.safe_work",
    "full.todo",
    "full.operator",
    "full.decisions",
    "full.git",
    "full.bump",
    "full.docs",
    "full.demo",
    "full.personal",
    "full.auth",
    "full.sidecar",
    "full.working_state",
    "full.learning",
    "full.git_craft",
)

_REMINDER_ORDER = (
    "reminder.final_message",
    "reminder.decisions",
    "reminder.git",
    "reminder.bump",
    "reminder.docs",
    "reminder.demo",
    "reminder.personal",
    "reminder.item_body",
    "reminder.board_client",
    "reminder.working_state",
    "reminder.todo",
    "reminder.operator",
)

_HEADINGS = {
    "full": "IMPORTANT — completion contract:",
    "reminder": "IMPORTANT — completion contract (short reminder; the full version from this session still applies unchanged):",
}


def render(kind: str, instance_path: Path = INSTANCE_CONTRACT_PATH,
           owner: str | None = None) -> str:
    """Render `full` or `reminder`; a missing instance file means generic core only."""
    if kind not in _HEADINGS:
        raise ValueError(f"Unknown contract variant: {kind}")
    rules = _core_rules(owner or _cfg.OWNER)
    rules.update(_instance_rules(instance_path))
    order = _FULL_ORDER if kind == "full" else _REMINDER_ORDER
    return "\n".join((_HEADINGS[kind], *(rules[key] for key in order if key in rules)))
