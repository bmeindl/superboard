import json

import pytest

import crew_exchange as crew
import gc_runner as runner


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for role in ("writer", "checker"):
        home = tmp_path / "crew" / "roles" / role
        home.mkdir(parents=True)
        (home / "ROLE.md").write_text(f"Instructions for {role}")
    (tmp_path / "crew/crew.json").write_text(json.dumps({
        "roles": {"writer": {"collaborators": ["checker"]}, "checker": {"collaborators": []}}
    }))
    monkeypatch.setattr(runner, "GC_ROOT", tmp_path)
    monkeypatch.setattr(runner._p, "DATA", tmp_path / ".superboard")
    monkeypatch.setattr(runner, "USAGE_LOG", tmp_path / "usage")
    return tmp_path


def pending(*asks):
    return {"addr": {"id": "a0b1c2"}, "title": "Draft",
            "thread": [{"kind": "ask", "author": "human", "text": question} for question in asks]}


def exchange(workspace, *asks):
    return crew.Exchange.for_card(pending(*asks), workspace, workspace / ".superboard")


def success(reply):
    return {"ok": True, "reply": reply, "denials": [], "session_id": "unused"}


def test_handover_real_processes_and_repeat(workspace, monkeypatch):
    # Exercise the supported Claude subprocess path with a deterministic CLI substitute.
    script = workspace / "runner.py"
    script.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
role = os.environ["SUPERBOARD_CREW_ROLE"]
turn = sys.stdin.read()
assert f"Instructions for {role}" in turn
events = json.loads(turn.split("Exchange so far (JSON):\\n")[-1])
if role == "checker":
    assert "private owner note" not in turn
messages = ["HANDOVER checker\\nInspect draft against notes", "QUESTION\\nWhich release?",
            "ANSWER\\nJune", "RETURN\\nFeature is beta", "REVISE\\nCite a source",
            "RETURN\\nnotes.md, line 5", "Corrected summary: beta (notes.md)."]
with open("observed", "a") as f: f.write(role + "\\n")
print(json.dumps({"type":"result","subtype":"success","result":messages[len(events)]}))
""")
    script.chmod(0o700)
    monkeypatch.setattr(runner, "STREAM", False)
    turn = exchange(workspace, "@writer Summarize")
    journal = runner.RunJournal("a0b1c2", "Draft", "", 120, journal_dir=workspace / "journal")
    out = runner._run_exchange(turn, "private owner note", str(script), 120, "", journal, None, {})
    assert out["ok"] and out["reply"].startswith("Corrected summary")
    assert out["session_id"] == ""
    assert (workspace / "observed").read_text().splitlines() == ["writer", "checker"] * 3 + ["writer"]
    repeat = runner._run_exchange(turn, "private owner note", "missing", 120, "", journal, None, {})
    assert repeat["reply"] == out["reply"]
    assert exchange(workspace, "@writer Summarize", "continue crew").path == turn.path
    assert exchange(workspace, "@writer Summarize", "Another release").path != turn.path
    assert exchange(workspace, "Ordinary card") is None


def test_recovery_delivers_a_finished_checker_once(workspace, monkeypatch):
    turn = exchange(workspace, "@writer Draft")
    turn.record([], "HANDOVER checker\nReview the draft")
    jd = workspace / "journal"
    journal = runner.RunJournal("a0b1c2", "Draft", "", 120, journal_dir=jd)
    journal.meta["crew"] = {"owner": "writer", "step": 1, "file": turn.path.name}
    journal._write()
    journal.out_path.write_text(json.dumps({"type": "result", "subtype": "success",
                                            "result": "RETURN\nBeta feature."}))
    monkeypatch.setattr(runner, "_open_gc_ids", lambda _: {"a0b1c2"})
    config = workspace / "crew/crew.json"
    roles = json.loads(config.read_text())["roles"]
    config.write_text(json.dumps({"roles": dict(reversed(roles.items()))}))
    posted = []
    monkeypatch.setattr(runner, "_post_append", lambda *args, **kw: posted.append(args[2]))
    monkeypatch.setattr(runner, "RECOVER_GRACE", 0)
    runner.recover_journals(journal_dir=jd, sidecar_dir=workspace)
    assert len(posted) == 1 and "continue crew" in posted[0] and "Beta feature" not in posted[0]
    assert turn.events()[-1]["from"] == "checker" and turn.events()[-1]["to"] == "writer"
    assert len(turn.events()) == 2
    # The same receipt was already saved before the process died.
    runner._recover_exchange(journal.meta, success("RETURN\nBeta feature."))
    assert len(turn.events()) == 2
    assert turn.turn(turn.events()) == "writer"


def test_invalid_instructions_do_not_dispatch(workspace):
    turn = exchange(workspace, "@writer Draft")
    with turn.locked(), pytest.raises(ValueError, match="Another Crew run"), turn.locked():
        pytest.fail("a second launcher acquired the same card")
    with pytest.raises(ValueError, match="collaborator"):
        turn.record([], "HANDOVER outsider\nReview")
    with pytest.raises(ValueError, match="preceding"):
        turn.record([], "REVISE\nPlease revise")
    turn.record([], "HANDOVER checker\nReview")
    with pytest.raises(ValueError, match="RETURN or QUESTION"):
        turn.record(turn.events(), "HANDOVER writer\nCircular request")
    assert len(turn.events()) == 1


def test_only_explicit_first_human_mention_binds(workspace):
    assert exchange(workspace, "@unknown Please review") is None
    assert exchange(workspace, "An ordinary ask", "@writer A later mention") is None
    for author in ("agent", "system", "unknown", None):
        card = pending("@writer Please delegate")
        card["thread"][0]["author"] = author
        assert crew.Exchange.for_card(card, workspace, workspace / ".superboard") is None


def test_uncertain_owner_post_is_idempotent(workspace, monkeypatch):
    import server

    board_path = workspace / "inbox" / "board.md"
    board_path.parent.mkdir()
    board_path.write_text("## Topic\n\n### Now\n\n- [ ] Draft *(2026-10-20)*\n"
                          "  @gc-id: a0b1c2\n  @gc: @writer verify\n")
    class LocalRequest:
        _gc_append = server.Handler._gc_append

        def _json(self, status, body):
            self.response = (status, body)

        def _atomic_write(self, text):
            self.board_path.write_text(text)

    handler = LocalRequest()
    handler.board_path = board_path
    identifier = "1" * 32
    payload = {"addr": {"id": "a0b1c2"}, "kind": "reply", "by": "agent",
               "text": "Checked release.", "delivery_id": identifier}
    handler._gc_append(payload)
    assert handler.response[0] == 200
    after_first = board_path.read_text()
    # The write landed, but the first caller never received its acknowledgement.
    jd = workspace / "journal"
    journal = runner.RunJournal("a0b1c2", "Draft", "", 60, journal_dir=jd)
    journal.meta["delivery_id"] = identifier
    journal.ready("Checked release.", "")
    monkeypatch.setattr(runner, "_open_gc_ids", lambda _: set())

    def redeliver(_url, card, text, *args, delivery_id):
        handler._gc_append({**payload, "text": text, "addr": {"id": card}, "delivery_id": delivery_id})

    monkeypatch.setattr(runner, "_post_append", redeliver)
    assert runner.recover_journals(journal_dir=jd, sidecar_dir=workspace)
    assert not journal.meta_path.exists()
    assert handler.response[0] == 200
    assert board_path.read_text() == after_first
    handler._gc_append({**payload, "text": "Different result"})
    assert handler.response[0] == 409
    assert board_path.read_text() == after_first


def test_removed_authority_prevents_next_launch(workspace):
    turn = exchange(workspace, "@writer Draft")
    turn.record([], "HANDOVER checker\nReview")
    config = workspace / "crew/crew.json"
    roles = json.loads(config.read_text())["roles"]
    roles["writer"]["collaborators"].clear()
    config.write_text(json.dumps({"roles": roles}))
    resumed = exchange(workspace, "@writer Draft", "continue crew")
    with pytest.raises(ValueError, match="no longer a collaborator"):
        resumed.prompt(resumed.events(), "")
    del roles["writer"]
    config.write_text(json.dumps({"roles": roles}))
    with pytest.raises(ValueError, match="no longer configured"):
        exchange(workspace, "@writer Draft", "continue crew")
