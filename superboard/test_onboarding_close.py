"""The final onboarding card preserves history before removing its topic."""

from __future__ import annotations

from pathlib import Path

import server
import sweep


def _done_item(title: str, gc_id: str, ref: str = "") -> dict:
    thread = [{"kind": "reply", "text": ref or f"Finished {title}"},
              {"kind": "done", "text": ""}]
    return {
        "title": title, "done": True, "date": "2026-08-23", "done_at": "2026-08-23T20:00:00Z",
        "body": [], "stages": [], "id": gc_id, "parent": "", "wait": "", "wait_since": "",
        "on": "", "thread": thread, "session": "", "sessions": [], "gc_last": "", "subs": [],
    }


def test_archive_completed_theme_preserves_items_and_sidecars(tmp_path: Path) -> None:
    board_path = tmp_path / "inbox" / "board.md"
    archive_path = board_path.parent / "board-archive.md"
    threads = board_path.parent / "gc-threads"
    archived_threads = threads / "archive"
    threads.mkdir(parents=True)
    sidecar = threads / "aaaaaaaaaaaa-20260823-200000-0000.md"
    sidecar.write_text("full answer", encoding="utf-8")

    first = _done_item(
        "Start here", "aaaaaaaaaaaa",
        "Done → full reply: inbox/gc-threads/aaaaaaaaaaaa-20260823-200000-0000.md",
    )
    closer = _done_item("Finish Getting started", "bbbbbbbbbbbb")
    board = {
        "header": ["# To-do Board"], "staging": [], "cockpit": [], "persons": [], "notes": [],
        "themes": [{"name": "Getting started", "cols": {
            "Jetzt": [first], "Bald": [], "Geparkt": [closer],
        }}],
    }
    board_path.write_text(server.serialize_board(board), encoding="utf-8")

    ok, note, count = sweep.archive_completed_theme(
        "Getting started", "bbbbbbbbbbbb", board_path, archive_path, threads, archived_threads,
    )

    assert (ok, note, count) == (True, "Getting started archived", 2)
    assert "## Getting started" not in board_path.read_text(encoding="utf-8")
    archived = archive_path.read_text(encoding="utf-8")
    assert "Start here" in archived and "Finish Getting started" in archived
    assert "inbox/gc-threads/archive/aaaaaaaaaaaa-20260823-200000-0000.md" in archived
    assert not sidecar.exists()
    assert (archived_threads / sidecar.name).read_text(encoding="utf-8") == "full answer"


def test_archive_completed_theme_refuses_an_open_step(tmp_path: Path) -> None:
    board_path = tmp_path / "board.md"
    archive_path = tmp_path / "board-archive.md"
    open_item = _done_item("Set up email", "aaaaaaaaaaaa")
    open_item["done"] = False
    closer = _done_item("Finish Getting started", "bbbbbbbbbbbb")
    board = {
        "header": ["# To-do Board"], "staging": [], "cockpit": [], "persons": [], "notes": [],
        "themes": [{"name": "Getting started", "cols": {
            "Jetzt": [open_item], "Bald": [], "Geparkt": [closer],
        }}],
    }
    board_path.write_text(server.serialize_board(board), encoding="utf-8")

    ok, note, count = sweep.archive_completed_theme(
        "Getting started", "bbbbbbbbbbbb", board_path, archive_path,
        tmp_path / "threads", tmp_path / "threads" / "archive",
    )

    assert ok is False and count == 0 and "Set up email" in note
    assert "## Getting started" in board_path.read_text(encoding="utf-8")
    assert not archive_path.exists()
