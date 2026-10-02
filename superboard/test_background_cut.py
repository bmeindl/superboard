"""Background jobs cut off at turn end (thread 0557fa238f8c, 22.09.2026).

Measured against claude 2.1.x headless `-p`: a turn that ends while a run_in_background task
is still running yields `result`, then `task_updated status=killed` + `task_notification
status=stopped`, then exit 0 — the model never gets another turn. The runner must notice
and resume the session once, so the thread receives the real result and not the interim
"waiting …" message. The event lines below mirror the measured stream (ids shortened).
"""
from __future__ import annotations

import json

import contract
import gc_runner as g


def _stream(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events)


SID = "dd7d59fa-f580-4bde-b205-13ecd55c7258"
INIT = {"type": "system", "subtype": "init", "session_id": SID, "model": "claude-haiku-4-5"}
STARTED = {"type": "system", "subtype": "task_started", "task_id": "bpo", "is_backgrounded": True,
           "task_type": "local_bash", "description": "gh run watch 123"}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "session_id": SID,
          "result": "Interim: waiting for the pipeline.", "usage": {}, "modelUsage": {}}
KILLED = {"type": "system", "subtype": "task_updated", "task_id": "bpo", "patch": {"status": "killed"}}
STOPPED = {"type": "system", "subtype": "task_notification", "task_id": "bpo", "status": "stopped"}
DONE = {"type": "system", "subtype": "task_notification", "task_id": "bpo", "status": "completed"}


def test_killed_after_result_is_orphaned():
    out = g._parse_claude_stdout(_stream([INIT, STARTED, RESULT, KILLED, STOPPED]), "", 0)
    assert out["ok"] and out["orphaned_tasks"] == [
        {"task_id": "bpo", "description": "gh run watch 123", "status": "stopped"}]


def test_stream_ending_right_after_result_is_orphaned_too():
    out = g._parse_claude_stdout(_stream([INIT, STARTED, RESULT]), "", 0)
    assert [t["task_id"] for t in out["orphaned_tasks"]] == ["bpo"]


def test_job_completed_before_result_is_fine():
    out = g._parse_claude_stdout(_stream([INIT, STARTED, DONE, RESULT]), "", 0)
    assert out["orphaned_tasks"] == []


def test_foreground_tool_and_old_single_object_format_yield_nothing():
    fg = {**STARTED, "is_backgrounded": False}
    assert g._parse_claude_stdout(_stream([INIT, fg, RESULT]), "", 0)["orphaned_tasks"] == []
    assert g._parse_claude_stdout(json.dumps({**RESULT, "type": "result"}), "", 0)["orphaned_tasks"] == []


class _Journal:
    def __init__(self):
        self.prompts = []

    def save_prompt(self, p):
        self.prompts.append(p)


def _interim():
    return {"ok": True, "reply": "Interim: waiting for the pipeline.\n\nDetails.", "session_id": SID,
            "denials": [], "context_tokens": 1, "usage_summary": {}, "raw_error": "",
            "orphaned_tasks": [{"task_id": "bpo", "description": "gh run watch 123", "status": "killed"}]}


def test_continuation_resumes_same_session_and_keeps_interim_visible(monkeypatch):
    calls = []

    def fake_spawn(prompt, resume_id, *a, **kw):
        calls.append((prompt, resume_id))
        return {"ok": True, "reply": "Deployed to prod, Jira Done.", "session_id": SID, "denials": [],
                "context_tokens": 2, "usage_summary": {}, "raw_error": "", "orphaned_tasks": []}

    monkeypatch.setattr(g, "spawn_agent", fake_spawn)
    j = _Journal()
    out = g._continue_after_background_cut(_interim(), "claude", 10, "opus", j)
    assert calls[0][1] == SID  # resumed, not fresh
    assert "gh run watch 123" in calls[0][0] and "FOREGROUND" in calls[0][0]
    assert j.prompts == [calls[0][0]]  # journal knows the prompt that really ran
    assert out["reply"].startswith("Deployed to prod, Jira Done.")  # first line = real result
    assert "Interim message was: «Interim: waiting for the pipeline.»" in out["reply"]
    assert out["bg_continuation"] == "done"


def test_continuation_never_loops(monkeypatch):
    monkeypatch.setattr(g, "spawn_agent", lambda *a, **kw: {
        "ok": True, "reply": "Still waiting.", "session_id": SID, "denials": [], "context_tokens": 0,
        "usage_summary": {}, "raw_error": "",
        "orphaned_tasks": [{"task_id": "x", "description": "again", "status": "killed"}]})
    out = g._continue_after_background_cut(_interim(), "claude", 10, "opus", _Journal())
    assert "⚠ The continuation ALSO ended" in out["reply"]


def test_failed_continuation_keeps_interim_but_marks_it(monkeypatch):
    monkeypatch.setattr(g, "spawn_agent", lambda *a, **kw: {
        "ok": False, "reply": "", "session_id": "", "denials": [], "context_tokens": 0,
        "usage_summary": {}, "raw_error": "exit 1"})
    out = g._continue_after_background_cut(_interim(), "claude", 10, "opus", _Journal())
    assert out["reply"].startswith("Interim: waiting for the pipeline.")
    assert "⚠ This is an INTERIM message" in out["reply"] and "exit 1" in out["reply"]
    assert out["bg_continuation"] == "failed"


def test_contract_carries_the_rule_in_both_variants():
    assert "NEVER end your turn while a background job" in g._contract_for("claude", "full", "opus")
    assert "Never end your turn with a background job" in g._contract_for("claude", "reminder", "opus")
    assert "full.no_background_wait" in contract._FULL_ORDER
