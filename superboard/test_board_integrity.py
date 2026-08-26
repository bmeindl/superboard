"""Regressionsschutz für den Verlust-Wächter.

Bis zum 25.08.2026 hatte `board_integrity.py` KEINEN eigenen Test — `test_server.py`
prüfte nur, dass der Cockpit-Payload einen `integrity`-Schlüssel trägt, nie, ob die
Erkennung tatsächlich erkennt. Ein Wächter ohne Test ist ein Versprechen ohne Deckung:
Genau er hat am 25.08. neun echt verlorene Items gefunden, und genau in ihm steckte
gleichzeitig ein stiller False-Negative-Pfad (siehe `test_archive_prose_mention_*`).
"""
from __future__ import annotations

import os
import time

import board_integrity as bi

WINDOW = bi.ORPHAN_WINDOW_DAYS * 86400


def _fixture(tmp_path, board_text: str, archive_text: str = "", thread_ids=()):
    board = tmp_path / "board.md"
    board.write_text(board_text)
    archive = tmp_path / "board-archive.md"
    archive.write_text(archive_text)
    threads = tmp_path / "gc-threads"
    threads.mkdir(exist_ok=True)
    for gc_id in thread_ids:
        f = threads / f"{gc_id}-20260825-120000-abcd.md"
        f.write_text(f"# Turn: irgendwas\n\n*Item @gc-id: {gc_id}*\n")
    return dict(board=board, archive=archive, threads=threads, root=tmp_path)


def _item(title: str, gc_id: str) -> str:
    return f"- [ ] {title} *(2026-08-25)*\n  @gc-id: {gc_id}\n"


def test_clean_board_is_silent(tmp_path):
    kw = _fixture(tmp_path, _item("A", "a1b2c3d4e5f6"), thread_ids=["a1b2c3d4e5f6"])
    assert bi.loss_issues(**kw) == []


def test_orphan_thread_file_is_reported(tmp_path):
    """Das Item ist aus board.md verschwunden, sein Faden lebt — der teure Fall."""
    kw = _fixture(tmp_path, "# Themen\n", thread_ids=["a1b2c3d4e5f6"])
    issues = bi.loss_issues(**kw)
    assert len(issues) == 1
    assert "a1b2c3d4e5f6" in issues[0]


def test_archived_item_is_not_an_orphan(tmp_path):
    """sweep.py verschiebt abgehakte Items ins Archiv — kein Verlust."""
    kw = _fixture(tmp_path, "# Themen\n",
                  archive_text=_item("A", "a1b2c3d4e5f6"), thread_ids=["a1b2c3d4e5f6"])
    assert bi.loss_issues(**kw) == []


def test_archive_prose_mention_does_not_mask_a_real_loss(tmp_path):
    """Der False-Negative-Pfad vom 25.08.: eine ID, die im Archiv nur ERWÄHNT wird.

    Vorher deckte die lose Hex-Suche über den ganzen Archivtext solche Zitate mit ab und
    nahm das Item dauerhaft aus der Prüfung — 21 von 199 aktiven Items waren betroffen.
    """
    prose = "  Herkunft: GC-Health-Check-Faden `a1b2c3d4e5f6`, 14.-16.08.2026.\n"
    kw = _fixture(tmp_path, "# Themen\n", archive_text=prose, thread_ids=["a1b2c3d4e5f6"])
    issues = bi.loss_issues(**kw)
    assert len(issues) == 1, "Erwähnung im Archiv-Fließtext darf keinen Verlust verdecken"


def test_old_orphan_outside_the_window_stays_quiet(tmp_path):
    """Bewusst gelöschte Alt-Items dürfen den Wächter nicht chronisch rot färben."""
    kw = _fixture(tmp_path, "# Themen\n", thread_ids=["a1b2c3d4e5f6"])
    f = next(kw["threads"].glob("*.md"))
    old = time.time() - WINDOW - 86400
    os.utime(f, (old, old))
    assert bi.loss_issues(**kw) == []


def test_dangling_gc_parent_is_reported(tmp_path):
    kw = _fixture(tmp_path, _item("Kind", "a1b2c3d4e5f6") + "  @gc-parent: 111122223333\n")
    issues = bi.loss_issues(**kw)
    assert any("111122223333" in i for i in issues)


def test_dead_sidecar_reference_is_reported(tmp_path):
    board = _item("A", "a1b2c3d4e5f6") + "  → voller Text: inbox/gc-threads/weg.md\n"
    kw = _fixture(tmp_path, board, thread_ids=["a1b2c3d4e5f6"])
    assert any("weg.md" in i for i in bi.loss_issues(**kw))


def test_orphan_message_warns_that_empty_git_history_is_not_exoneration(tmp_path):
    """Der Hinweistext ist der einzige Entscheidungshelfer, den ein Operator bekommt.

    Am 25.08. hat sein damaliger Wortlaut (`git log -S …` als alleiniger Prüfschritt) einen
    Lauf dazu gebracht, neun True Positives als Fehlalarm abzutun: nie committete Items
    liefern null Treffer, und null Treffer las sich wie „gab es nie".
    """
    kw = _fixture(tmp_path, "# Themen\n", thread_ids=["a1b2c3d4e5f6"])
    msg = bi.loss_issues(**kw)[0]
    assert "gc-threads/a1b2c3d4e5f6" in msg, "der Faden selbst muss als Unterscheider genannt sein"
    assert "reflog" in msg or "fsck" in msg, "leere Git-Historie braucht einen Folgeschritt"


def test_checked_item_without_a_date_is_hygiene_not_loss(tmp_path):
    board = "- [x] Fertig ohne Datum\n  @gc-id: a1b2c3d4e5f6\n"
    kw = _fixture(tmp_path, board)
    assert bi.loss_issues(**kw) == []
    assert any("a1b2c3d4e5f6" in i for i in bi.hygiene_issues(board=kw["board"]))
