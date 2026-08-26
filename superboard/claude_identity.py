"""Explicit, non-secret Claude identity boundaries for Board subprocesses.

The Board server is long-lived and may be started from a shell with temporary account
overrides. Superboard's plain Claude profile deliberately means the CLI's default login,
so a parent process cannot silently reroute future board content.

The default login means no CLAUDE_CONFIG_DIR at all. Setting it — even to
``~/.claude`` — moves the account state file from ``~/.claude.json`` to
``<config-dir>/.claude.json`` and thereby opens a separate, logged-out login namespace
("Not logged in · Please run /login"). The boundary is
therefore enforced by REMOVING all account selectors, never by pinning the directory.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

ACCOUNT_ENV_KEYS = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)

IDENTITY_BY_RUNNER = {
    "claude": "claude-default",
    "codex": "codex",
}


def without_claude_account_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Remove all Claude account selectors/secrets from an inherited environment."""
    env = dict(os.environ if source is None else source)
    for key in ACCOUNT_ENV_KEYS:
        env.pop(key, None)
    return env


def default_claude_env(source: Mapping[str, str] | None = None, **extra: str) -> dict[str, str]:
    """Return an environment resolving to the Claude CLI's default login."""
    env = without_claude_account_env(source)
    env.update(extra)
    return env


def default_shell_prelude() -> str:
    """Shell statements that enforce the same boundary inside a tmux server environment."""
    return "unset " + " ".join(ACCOUNT_ENV_KEYS) + "; "


def identity_for_runner(runner: str) -> str:
    """Safe observability label for a selected execution lane, never an auth claim."""
    return IDENTITY_BY_RUNNER.get(runner, "unknown")


# A session handle is resumable only in the transcript store that created it. The
# public package exposes one Claude lane, always backed by the default store.


def claude_config_dir(runner: str) -> Path | None:
    """Config directory of a Claude lane, or None for non-Claude runners (codex).

    ``GC_CLAUDE_STORE`` only relocates where we LOOK for a transcript; it is
    never exported to the CLI.  Pinning ``CLAUDE_CONFIG_DIR`` would log the private lane
    out (see module docstring) — reading and authenticating are different questions.
    """
    if runner != "claude":
        return None
    return Path(os.environ.get("GC_CLAUDE_STORE") or (Path.home() / ".claude"))


def project_slug(cwd: str | os.PathLike[str]) -> str:
    """Claude Code's transcript folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))


def session_transcript(runner: str, session_id: str,
                       cwd: str | os.PathLike[str]) -> Path | None:
    """Path of a session transcript IF it exists in this lane's store, else None."""
    cfg = claude_config_dir(runner)
    if cfg is None or not session_id:
        return None
    path = cfg / "projects" / project_slug(cwd) / f"{session_id}.jsonl"
    return path if path.is_file() else None
