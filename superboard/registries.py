"""Fail-soft loaders for the local action and ritual registries.

The JSON files are trusted instance configuration, but a typo must not crash the
local server. Runtime callers receive valid entries plus diagnostics; tests use
the same diagnostics as a strict gate for checked-in files.

There is deliberately no plugin discovery or meta-schema. Two explicit loaders
keep the fields the board actually reads easy to understand and change.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PROOF_KINDS = {"none", "single", "multiline"}


def _read_object(path: Path, label: str) -> tuple[dict | None, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{label} is missing"]
    except (json.JSONDecodeError, OSError) as exc:
        return None, [f"{label} is unreadable: {exc}"]
    if not isinstance(raw, dict):
        return None, [f"{label}: top level must be an object"]
    return raw, []


def _text(value: object, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and (allow_empty or bool(value.strip()))


def load_actions(path: Path) -> tuple[list[dict], list[str]]:
    """Return validated actions in file order plus readable diagnostics."""
    raw, errors = _read_object(path, "actions.json")
    if raw is None:
        return [], errors
    entries = raw.get("actions")
    if not isinstance(entries, list):
        return [], [*errors, "actions.json: 'actions' must be a list"]

    valid: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"actions.json: actions[{index}]"
        problems: list[str] = []
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        key = entry.get("key")
        if not _text(key) or not SLUG_RE.fullmatch(key):
            problems.append("'key' must be a lowercase slug")
        elif key in seen:
            problems.append(f"duplicate key '{key}'")
        if not _text(entry.get("label")):
            problems.append("'label' must be non-empty text")
        if not _text(entry.get("prompt")):
            problems.append("'prompt' must be non-empty text")
        if "auth" in entry and not isinstance(entry["auth"], bool):
            problems.append("'auth' must be a boolean")
        for field in ("icon", "group", "rhythm", "status", "schedule"):
            if field in entry and not _text(entry[field], allow_empty=True):
                problems.append(f"'{field}' must be text")
        clean = dict(entry)
        if "timeout" in entry and (
            not isinstance(entry["timeout"], int) or isinstance(entry["timeout"], bool)
            or entry["timeout"] <= 0
        ):
            # Rückwärtskompatibler Fail-soft-Fall: der Server fiel hier schon immer
            # auf seinen Default zurück. Die Diagnose bleibt sichtbar, die Action
            # selbst aber benutzbar.
            errors.append(f"{where}: 'timeout' must be a positive integer; using the default")
            clean.pop("timeout", None)
        endpoint = entry.get("run_endpoint")
        if endpoint is not None and (not _text(endpoint) or not endpoint.startswith("/api/")):
            # Gleiches Fail-soft-Verhalten wie bisher im API-Serializer: fremde URLs
            # verschwinden, die Action bleibt als normale /api/action-run-Action da.
            errors.append(f"{where}: 'run_endpoint' must start with /api/; field ignored")
            clean.pop("run_endpoint", None)
        if problems:
            errors.extend(f"{where}: {problem}" for problem in problems)
            continue
        seen.add(key)
        valid.append(clean)
    return valid, errors


def _weekday_time(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("weekday"), int)
        and not isinstance(value.get("weekday"), bool)
        and 0 <= value["weekday"] <= 6
        and _text(value.get("time"))
        and bool(TIME_RE.fullmatch(value["time"]))
    )


def load_rituals(path: Path) -> tuple[dict, list[str]]:
    """Return valid ritual config; omit individual invalid definitions."""
    filename = path.name
    raw, errors = _read_object(path, filename)
    if raw is None:
        return {}, errors
    active_from = raw.get("active_from", "")
    if active_from and isinstance(active_from, str):
        try:
            date.fromisoformat(active_from)
        except ValueError:
            errors.append(f"{filename}: 'active_from' must be YYYY-MM-DD")
            active_from = ""
    elif active_from != "":
        errors.append(f"{filename}: 'active_from' must be YYYY-MM-DD")
        active_from = ""
    # `rituale` is the pre-public key. Accept it for existing workspaces, but all new
    # starter files use the English `rituals` key.
    rituals = raw.get("rituals", raw.get("rituale"))
    if not isinstance(rituals, dict):
        return {"active_from": active_from, "rituale": {}}, [
            *errors, f"{filename}: 'rituals' must be an object",
        ]

    valid: dict[str, dict] = {}
    for rid, entry in rituals.items():
        where = f"{filename}: rituals.{rid}"
        problems: list[str] = []
        if not _text(rid) or not SLUG_RE.fullmatch(rid):
            problems.append("id must be a lowercase slug")
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        kind = entry.get("kind")
        if kind not in {"daily", "weekly"}:
            problems.append("'kind' must be daily or weekly")
        if not _text(entry.get("title")):
            problems.append("'title' must be non-empty text")
        proof = entry.get("proof", "single")
        if proof not in PROOF_KINDS:
            problems.append("'proof' must be none, single, or multiline")
        if proof != "none" and not _text(entry.get("prompt")):
            problems.append("'prompt' must be non-empty text for rituals with proof")
        if "persist_personal" in entry and not _text(entry["persist_personal"], allow_empty=True):
            problems.append("'persist_personal' must be text")
        if "rotation" in entry:
            rot = entry["rotation"]
            ok = isinstance(rot, dict) and isinstance(rot.get("prompts"), list) and rot["prompts"] \
                and all(_text(x) for x in rot["prompts"]) and _text(rot.get("start")) \
                and isinstance(rot.get("days", 7), int) and rot.get("days", 7) >= 1
            if ok:
                try:
                    date.fromisoformat(rot["start"])
                except ValueError:
                    ok = False
            if not ok:
                problems.append("'rotation' needs start YYYY-MM-DD, days>=1 and a non-empty prompts list")

        if kind == "daily":
            deadline = entry.get("deadline")
            appears = entry.get("appears", "06:00")
            if not _text(deadline) or not TIME_RE.fullmatch(deadline):
                problems.append("daily 'deadline' must use HH:MM")
            if not _text(appears) or not TIME_RE.fullmatch(appears):
                problems.append("daily 'appears' must use HH:MM")
        elif kind == "weekly":
            if not _weekday_time(entry.get("appears")):
                problems.append("weekly 'appears' needs weekday 0..6 and an HH:MM time")
            if not _weekday_time(entry.get("deadline")):
                problems.append("weekly 'deadline' needs weekday 0..6 and an HH:MM time")

        if problems:
            errors.extend(f"{where}: {problem}" for problem in problems)
            continue
        valid[rid] = entry
    return {"active_from": active_from, "rituale": valid}, errors
