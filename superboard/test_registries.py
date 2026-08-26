"""Schema gates for the instance registries and their fail-soft runtime behavior."""

from __future__ import annotations

import json
from pathlib import Path

import registries

HERE = Path(__file__).resolve().parent


def _write(tmp_path: Path, name: str, value: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_checked_in_registries_are_strictly_valid() -> None:
    actions, action_errors = registries.load_actions(HERE / "actions.json")
    rituals, ritual_errors = registries.load_rituals(HERE / "rituals.json")
    assert action_errors == []
    assert ritual_errors == []
    # A fresh workspace learns actions and rituals during onboarding instead of
    # inheriting somebody else's repeatable jobs.
    assert actions == []
    assert rituals["rituale"] == {}


def test_actions_keep_valid_neighbors_and_report_bad_entries(tmp_path: Path) -> None:
    valid = {"key": "good", "label": "Good", "auth": False, "prompt": "Run it."}
    path = _write(tmp_path, "actions.json", {"actions": [
        valid,
        {**valid, "label": "Duplicate"},
        {**valid, "key": "bad-endpoint", "run_endpoint": "https://example.com"},
        {**valid, "key": "bad-timeout", "timeout": True},
        "not-an-object",
    ]})
    actions, errors = registries.load_actions(path)
    assert [action["key"] for action in actions] == ["good", "bad-endpoint", "bad-timeout"]
    assert "run_endpoint" not in actions[1]
    assert "timeout" not in actions[2]
    joined = "\n".join(errors)
    assert "duplicate key 'good'" in joined
    assert "run_endpoint" in joined
    assert "timeout" in joined
    assert "must be an object" in joined


def test_actions_reject_malformed_and_wrong_top_level(tmp_path: Path) -> None:
    malformed = tmp_path / "actions.json"
    malformed.write_text("{ broken", encoding="utf-8")
    assert registries.load_actions(malformed)[0] == []
    wrong = _write(tmp_path, "list.json", [])
    actions, errors = registries.load_actions(wrong)
    assert actions == [] and "top level" in errors[0]


def test_rituals_keep_valid_neighbors_and_report_bad_entries(tmp_path: Path) -> None:
    daily = {"kind": "daily", "title": "Daily", "deadline": "11:00",
             "proof": "single", "prompt": "What happened?"}
    weekly = {"kind": "weekly", "title": "Weekly",
              "appears": {"weekday": 1, "time": "17:00"},
              "deadline": {"weekday": 2, "time": "17:00"},
              "proof": "none"}
    path = _write(tmp_path, "rituals.json", {
        "active_from": "2026-01-01",
        "rituals": {
            "daily": daily,
            "weekly": weekly,
            "bad-time": {**daily, "deadline": "tomorrow"},
            "bad-proof": {**daily, "proof": "essay"},
            "bad-object": [],
        },
    })
    config, errors = registries.load_rituals(path)
    assert config["active_from"] == "2026-01-01"
    assert set(config["rituale"]) == {"daily", "weekly"}
    joined = "\n".join(errors)
    assert "deadline" in joined
    assert "proof" in joined
    assert "must be an object" in joined


def test_rituals_reject_wrong_shapes_without_throwing(tmp_path: Path) -> None:
    path = _write(tmp_path, "rituals.json", {"active_from": "soon", "rituals": []})
    config, errors = registries.load_rituals(path)
    assert config == {"active_from": "", "rituale": {}}
    assert any("active_from" in error for error in errors)
    assert any("'rituals' must be an object" in error for error in errors)


def test_legacy_rituale_file_and_key_remain_readable(tmp_path: Path) -> None:
    path = _write(tmp_path, "rituale.json", {"active_from": "", "rituale": {}})
    config, errors = registries.load_rituals(path)
    assert errors == []
    assert config == {"active_from": "", "rituale": {}}
