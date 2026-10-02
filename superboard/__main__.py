"""Console entry point: `superboard <workspace>` serves a high-level board home.

Bootstraps a minimal workspace on first run (an empty board plus the runtime
data directory), then hands over to server.serve(). The server itself never
creates workspaces. A first start names the workspace explicitly; plain
`superboard` remains the convenient restart command from an existing workspace.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import hashlib
import secrets
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from superboard.onboarding import STARTER_ITEMS

PKG = Path(__file__).resolve().parent

WORKSPACE_STARTER_FILES = {
    "actions.json": PKG / "actions.json",
    "rituals.json": PKG / "rituals.json",
    "board.config.json": PKG / "board.config.example.json",
    ".claude/skills/superboard/SKILL.md": PKG / "superboard-skill.md",
    # Shipped alongside the one bundled cockpit action: the "Check for updates" card
    # is only a prompt, so the skill it names has to exist in a stranger's workspace
    # on day one. Same create-only rule as every other starter file.
    ".claude/skills/superboard-update/SKILL.md": PKG / "superboard-update-skill.md",
}

# Starter files are create-only for the USER: once someone edits a seeded skill it is
# theirs and no upgrade touches it. A copy the user never edited is a different case —
# leaving it at the old text would mean every workspace keeps the update skill of the
# release it happened to start on (verified 2026-09-06: seeding was strictly
# create-only, so a corrected skill never reached existing workspaces). Provenance is
# a stamp per seeded file (sha256 of what WE wrote, in `.superboard/starter-stamps.json`):
# workspace content == stamp → untouched → refresh. Workspaces that predate the stamps
# are covered by the hashes of the texts earlier releases actually shipped.
STARTER_STAMPS = "starter-stamps.json"
REFRESHABLE_STARTERS = {
    ".claude/skills/superboard/SKILL.md": {
        "240f6ec895e903b18cab36ab21cac3dbb61adc10dcdce257d4305ff4e0e1d80a",  # 0.1.0 and 0.2.0
    },
    ".claude/skills/superboard-update/SKILL.md": {
        "8823d91edf6e0600542df7639fb94c8169be3b3325265c4140a4f5ebd08987b7",  # 0.2.0
    },
}

STARTER_HEADER = """# Board

<!-- Format (strict, read AND written by superboard):
     "## Topic" = row of the matrix - "### Now|Next|Backlog" = column
     "- [ ] Title *(YYYY-MM-DD)*" = item (date = created/last moved)
       "- [ ] **Title**" = highlighted
       indented lines without checkbox = body - "  - [ ] ..." = sub-item
       a body line of only "···" splits short text from the deep dive
     "# To discuss" = per-person discussion lists - "# Notes" = free notes -->

## My to-dos

### Now

### Next

### Backlog

## Getting started
"""

STARTER_FOOTER = """
# To discuss

# Notes
"""

COLUMNS = ("Jetzt", "Bald", "Geparkt")
FILE_COLUMN_NAMES = {"Jetzt": "Now", "Bald": "Next", "Geparkt": "Backlog"}


def _starter_item(title: str, created: str, short: str, mission: str, ask: str, gc_id: str) -> str:
    body = [short, "···", mission]
    lines = [f"- [ ] {title} *({created})*"]
    for paragraph in body:
        lines.append(f"  {paragraph}")
    lines.append(f"  @gc-id: {gc_id}")
    lines.append(f"  @gc: {ask}")
    return "\n".join(lines)


def _starter_board(today: date | None = None, gc_ids: list[str] | None = None) -> str:
    created = (today or date.today()).isoformat()
    ids = list(gc_ids or [])
    while len(ids) < len(STARTER_ITEMS):
        ids.append(secrets.token_hex(6))
    out = [STARTER_HEADER]
    for column in COLUMNS:
        out.append(f"### {FILE_COLUMN_NAMES[column]}\n")
        entries = [
            _starter_item(title, created, short, mission, ask, ids[index])
            for index, (col, title, short, mission, ask) in enumerate(STARTER_ITEMS)
            if col == column
        ]
        if entries:
            out.append("\n".join(entries) + "\n")
    return "\n".join(out) + STARTER_FOOTER


def _bootstrap(root: Path) -> Path:
    board = Path(os.environ.get("GC_BOARD", "").strip() or root / "inbox" / "board.md")
    if not board.exists():
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_text(_starter_board(), encoding="utf-8")
    (board.parent / "gc-threads").mkdir(exist_ok=True)
    for relative, source in WORKSPACE_STARTER_FILES.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The public filename changed, but an existing workspace owns its ritual
        # content. Seed the new path from the legacy file before the packaged empty
        # starter can win path selection and make those rituals appear to vanish.
        if relative == "rituals.json" and not destination.exists():
            legacy = root / "rituale.json"
            if legacy.is_file():
                destination.write_bytes(legacy.read_bytes())
                continue
        content = source.read_bytes()
        try:
            with destination.open("xb") as target:
                target.write(content)
        except FileExistsError:
            _refresh_untouched_starter(root, relative, destination, content)
            continue
        if relative in REFRESHABLE_STARTERS:
            _stamp(root, relative, content)
    _offer_default_actions(root)
    try:
        with (root / ".gitignore").open("x", encoding="utf-8") as ignore:
            ignore.write(".superboard/\n")
    except FileExistsError:
        pass
    data = Path(os.environ.get("GC_DATA", "").strip() or root / ".superboard")
    (data / "journal").mkdir(parents=True, exist_ok=True)
    # The board client is product mechanics, not user-owned state: it is REFRESHED on
    # every start rather than create-once, so an upgraded server never leaves an
    # agent holding an older client. It lives in the workspace (and not only in the
    # package) because the agent's shell is a separate process that may not be able to
    # import superboard at all — a write path that only sometimes exists is one the
    # agent will route around by hand-editing board.md, breaking the single-writer rule.
    # The command named by every prompt is workspace-stable even when runtime data
    # (journals/caches) is redirected elsewhere with GC_DATA.
    client = root / ".superboard" / "board_write.py"
    client.parent.mkdir(parents=True, exist_ok=True)
    client.write_bytes((PKG / "board_write.py").read_bytes())
    return board


def _stamps_path(root: Path) -> Path:
    return root / ".superboard" / STARTER_STAMPS


def _read_stamps(root: Path) -> dict:
    try:
        loaded = json.loads(_stamps_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    # A structurally odd stamps file must never break a start: keep only the shapes
    # the readers below rely on.
    files = loaded.get("files")
    offered = loaded.get("offered_actions")
    return {
        "files": {k: v for k, v in files.items() if isinstance(v, str)} if isinstance(files, dict) else {},
        "offered_actions": [k for k in offered if isinstance(k, str)] if isinstance(offered, list) else [],
    }


def _write_atomic(path: Path, text: str) -> None:
    """Sibling temp file + os.replace: a crash mid-write never truncates the target."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_stamps(root: Path, stamps: dict) -> None:
    path = _stamps_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(stamps, indent=2, sort_keys=True) + "\n")


def _stamp(root: Path, relative: str, content: bytes) -> None:
    stamps = _read_stamps(root)
    stamps.setdefault("files", {})[relative] = hashlib.sha256(content).hexdigest()
    _write_stamps(root, stamps)


def _refresh_untouched_starter(root: Path, relative: str, destination: Path, content: bytes) -> None:
    """Replace a seeded starter only when the workspace copy is provably ours."""
    if relative not in REFRESHABLE_STARTERS or destination.is_symlink():
        return  # a symlink points at something that is not our seeded copy
    try:
        current = hashlib.sha256(destination.read_bytes()).hexdigest()
    except OSError:
        return
    shipped = hashlib.sha256(content).hexdigest()
    known = set(REFRESHABLE_STARTERS[relative])
    stamped = _read_stamps(root).get("files", {}).get(relative)
    if stamped:
        known.add(stamped)
    if current == shipped:
        if stamped != shipped:
            _stamp(root, relative, content)
        return
    if current not in known:
        return  # edited by the user: theirs, untouched
    _write_atomic(destination, content.decode("utf-8"))
    _stamp(root, relative, content)
    print(f"superboard: refreshed {relative} (untouched starter, new release text)", flush=True)


def _offer_default_actions(root: Path) -> None:
    """Add a shipped Cockpit card ONCE to an actions.json that never had it.

    `actions.json` is user-owned, so the rule is narrow: a default key is appended when
    it is absent AND this workspace was never offered it (`.superboard/starter-stamps.json`
    remembers every offer). A card the user deleted therefore stays deleted for as long
    as that stamps file exists. Existing entries keep their order and content; the file
    is re-serialised canonically (2-space JSON, UTF-8), written atomically. An
    unparsable file is left alone."""
    try:
        defaults = json.loads((PKG / "actions.json").read_text(encoding="utf-8")).get("actions", [])
    except (OSError, ValueError):
        return
    default_keys = [a.get("key") for a in defaults if isinstance(a, dict) and a.get("key")]
    stamps = _read_stamps(root)
    offered = set(stamps.get("offered_actions", []))
    missing_offers = [k for k in default_keys if k not in offered]
    if not missing_offers:
        return
    path = root / "actions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    entries = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return
    present = {a.get("key") for a in entries if isinstance(a, dict)}
    added = [a for a in defaults if a.get("key") in missing_offers and a.get("key") not in present]
    if added:
        entries.extend(added)
        _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("superboard: added shipped Cockpit card(s) " + ", ".join(a["key"] for a in added), flush=True)
    stamps["offered_actions"] = sorted(offered | set(default_keys))
    _write_stamps(root, stamps)


def _workspace_from_args(workspace: str | None) -> tuple[Path, bool]:
    """Resolve the board home and say whether the user named it explicitly."""
    if workspace:
        return Path(workspace).expanduser().resolve(), True
    configured = os.environ.get("GC_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(), True
    return Path.cwd().resolve(), False


def _claude_status(root: Path) -> tuple[str, str]:
    """Preflight wrapper — the implementation lives in server.runner_status so the
    terminal line and the browser's runner banner can never drift apart."""
    sys.path.insert(0, str(PKG))
    import server

    return server.runner_status(root)


def _repo_boundary(root: Path) -> Path | None:
    """Find a git root containing the chosen workspace without invoking git."""
    start = root if root.exists() else root.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _print_preflight(root: Path, fresh: bool) -> None:
    state = "new workspace" if fresh else "existing workspace"
    print(f"superboard: workspace {root}  ({state})")
    repo = _repo_boundary(root)
    if repo is not None and repo == root:
        print(
            "superboard: note — this workspace is itself a git repository. For a personal "
            "task host, a higher-level home may be more useful."
        )
    elif repo is not None:
        print(f"superboard: note — this path sits inside git repository {repo}")
    _status, message = _claude_status(root)
    print(f"superboard: {message}")
    print(
        "superboard: agent runs inherit the selected CLI's host access, MCP/provider "
        "configuration, and auto-mode permissions; ask the agent for exact terminal "
        "handoff steps whenever interaction is required."
    )


def run() -> None:
    ap = argparse.ArgumentParser(
        description="Serve a Superboard workspace. Name the workspace explicitly on first run."
    )
    ap.add_argument("workspace", nargs="?", help="high-level workspace directory")
    ap.add_argument("--port", type=int, default=47822)
    try:
        public_version = importlib.metadata.version("superboard")
    except importlib.metadata.PackageNotFoundError:
        public_version = "source checkout"
    ap.add_argument("--version", action="version", version=f"superboard {public_version}")
    ap.add_argument("--file", type=Path, default=None, help="advanced: serve this board.md")
    ap.add_argument(
        "--allow-code-repo",
        action="store_true",
        help="confirm an intentional first install at the root of a Git repository",
    )
    args = ap.parse_args()

    root, explicit = _workspace_from_args(args.workspace)
    default_board = root / "inbox" / "board.md"
    selected_board = Path(args.file.expanduser().resolve()) if args.file else default_board
    if not explicit and not default_board.exists() and args.file is None:
        ap.error(
            "first start needs an explicit workspace path, for example: "
            "superboard ~/Superboard"
        )
    repo = _repo_boundary(root)
    if not selected_board.exists() and repo == root and not args.allow_code_repo:
        ap.error(
            f"{root} is a Git repository root. Superboard is normally a high-level home; "
            "choose its parent or a separate directory. If this is intentional, repeat with "
            "--allow-code-repo."
        )
    root.mkdir(parents=True, exist_ok=True)
    os.environ["GC_ROOT"] = str(root)
    if args.file is not None:
        os.environ["GC_BOARD"] = str(args.file.expanduser().resolve())
    selected_board = Path(os.environ.get("GC_BOARD", selected_board))
    _print_preflight(root, fresh=not selected_board.exists())

    board = _bootstrap(root)
    import server
    server.serve(args.port, board)


if __name__ == "__main__":
    run()
