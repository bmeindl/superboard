"""Git state used by the board runner, independent from optional telemetry.

The prompt's Git block and the anchor between thread turns are core behavior.
They must not depend on whether the optional run-receipt extension is present.
Every function degrades to empty data because Git observability must never stop
an agent run.
"""

from __future__ import annotations

import subprocess

import paths as _p

GC_ROOT = _p.GC_ROOT
_GIT_TIMEOUT = 10


def _git(*args: str) -> str:
    """Run Git in the instance root and return an empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=GC_ROOT, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - Git context must never abort a run
        return ""


def git_head() -> str:
    return _git("rev-parse", "HEAD")


def _porcelain() -> list[str]:
    return [line for line in _git("status", "--porcelain").splitlines() if line]


def git_facts(status_max: int = 15, commits: int = 5) -> dict:
    """Compact worktree snapshot for a fresh agent prompt."""
    dirty = _porcelain()
    log = _git("log", "--oneline", "--no-decorate", f"-{commits}")
    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "status": dirty[:status_max],
        "status_more": max(0, len(dirty) - status_max),
        "commits": [line for line in log.splitlines() if line],
    }


def snapshot() -> dict:
    """Commit plus already-dirty files as a later delta baseline."""
    return {"head": git_head(), "dirty": [_dirty_path(line) for line in _porcelain()]}


def git_delta(before: str | dict) -> dict:
    """Commits and worktree changes since a SHA or :func:`snapshot`."""
    if isinstance(before, dict):
        head_before = before.get("head") or ""
        dirty_before = set(before.get("dirty") or [])
    else:
        head_before, dirty_before = before or "", None
    if not head_before:
        return {}

    head = _git("rev-parse", "HEAD")
    commits = [
        line for line in _git(
            "log", "--oneline", "--no-decorate", f"{head_before}..HEAD",
        ).splitlines() if line
    ]
    stat = _git("diff", "--stat", f"{head_before}..HEAD") if head != head_before else ""
    dirty = _porcelain()
    out = {"head": head, "commits": commits, "diffstat": stat, "dirty": dirty}
    if dirty_before is not None:
        paths = [_dirty_path(line) for line in dirty]
        out["dirty_new"] = [path for path in paths if path not in dirty_before]
        out["dirty_pre"] = sum(1 for path in paths if path in dirty_before)
    return out


def _dirty_path(line: str) -> str:
    return line.strip().split(" ", 1)[-1].strip()
