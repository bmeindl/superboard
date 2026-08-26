"""Canonical filesystem layout for an installed Superboard workspace.

The workspace defaults to the directory where ``superboard`` is started. All
user content lives there; package files remain read-only. ``GC_ROOT``,
``GC_BOARD`` and ``GC_DATA`` provide explicit overrides for launchers, tests and
isolated demo environments.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    """Env-Override oder Default. Leergesetzt zählt als nicht gesetzt."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


# Der Code-Ordner selbst (index.html, actions.json, … liegen hier).
HERE = Path(__file__).resolve().parent

# Workspace root: where the user's board lives. In the origin instance this was
# derived from the repo layout; as an installable tool the workspace is the
# current working directory — `superboard` serves the board of the directory it
# is started in. Override with GC_ROOT.
GC_ROOT = _env_path("GC_ROOT", Path.cwd())

INBOX = GC_ROOT / "inbox"
BOARD = _env_path("GC_BOARD", INBOX / "board.md")
# Bewusst aus BOARD abgeleitet, nicht aus GC_ROOT: wer das Board umlenkt, will das
# Archiv und die Fäden daneben haben, nicht im Original-Repo.
ARCHIVE = BOARD.parent / "board-archive.md"
THREADS = BOARD.parent / "gc-threads"
THREADS_ARCHIVE = THREADS / "archive"
RECEIPTS = BOARD.parent / "gc-receipts"

LOGS = GC_ROOT / "logs"

# Laufzeitdaten (gitignored, wachsen im Betrieb). Kept inside the workspace so
# an installed package directory stays read-only and runtime data survives
# reinstalls. Override with GC_DATA.
DATA = _env_path("GC_DATA", GC_ROOT / ".superboard")
JOURNAL = DATA / "journal"
USAGE_LOG = DATA / "usage-log.jsonl"

# Mutable instance files belong to the workspace, never to the installed package.
# The console bootstrap creates generic starting copies once and upgrades never
# overwrite them. Keeping these paths here prevents server/config/contract drift.
ACTIONS = GC_ROOT / "actions.json"
# New workspaces use the English filename. Existing workspaces keep working without a
# migration: prefer the legacy file only when it exists and the new name does not.
_RITUALS = GC_ROOT / "rituals.json"
_LEGACY_RITUALS = GC_ROOT / "rituale.json"
RITUALS = _LEGACY_RITUALS if _LEGACY_RITUALS.exists() and not _RITUALS.exists() else _RITUALS
CONFIG = GC_ROOT / "board.config.json"
CONTRACT = GC_ROOT / "board.contract.md"
