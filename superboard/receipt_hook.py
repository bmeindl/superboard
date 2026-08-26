"""Optional boundary to the run-receipt extension.

The runner imports only this stable no-op interface. If ``receipt.py`` is absent,
prompt building, agent execution, Git anchoring and reply posting keep working.
Errors in an installed extension are reported on stderr but remain telemetry
failures rather than board failures.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

_UNSET = object()
_IMPLEMENTATION: ModuleType | None | object = _UNSET
_WARNED: set[str] = set()


def _implementation() -> ModuleType | None:
    global _IMPLEMENTATION
    if _IMPLEMENTATION is _UNSET:
        try:
            _IMPLEMENTATION = importlib.import_module("receipt")
        except ModuleNotFoundError as exc:
            if exc.name != "receipt":
                raise
            _IMPLEMENTATION = None
    return _IMPLEMENTATION if isinstance(_IMPLEMENTATION, ModuleType) else None


def _warn(operation: str, exc: Exception) -> None:
    message = f"todo-board: Receipt-Hook {operation} fehlgeschlagen: {exc}"
    if message not in _WARNED:
        _WARNED.add(message)
        print(message, file=sys.stderr)


def write(gc_id: str, title: str, out: dict, git_before: str | dict,
          started: float) -> Path | None:
    """Write a receipt, or return ``None`` if unavailable or broken."""
    try:
        implementation = _implementation()
        return implementation.write(gc_id, title, out, git_before, started) if implementation else None
    except Exception as exc:  # noqa: BLE001 - optional telemetry never breaks a run
        _warn("write", exc)
        return None


def files(gc_id: str) -> list[Path]:
    """Return all item receipts, oldest first, or an empty list without the hook."""
    try:
        implementation = _implementation()
        if not implementation:
            return []
        return implementation.receipt_files(implementation.RECEIPT_DIR, gc_id)
    except Exception as exc:  # noqa: BLE001 - endpoint degrades to 404 instead of 500
        _warn("read", exc)
        return []
