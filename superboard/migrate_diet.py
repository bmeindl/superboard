#!/usr/bin/env python3
"""Einmalige board.md-Diät-Migration (beschlossen 2026-07-16, Konzept:
inbox/analyses/2026-07-16_board-md-diaet-konzept.md §3/1c).

Lagert BESTEHENDE überlange Faden-Turns (>INLINE_MAX Zeichen, @gc: wie @gc-re:)
in Sidecar-Dateien unter inbox/gc-threads/ aus — exakt die Regel, die server.py
für NEUE Turns seit der Diät anwendet. Nichts wird gekürzt: Volltext in die Datei,
inline bleibt Kurzsatz + typisierter Verweis.

Sicherheitsnetz (ext. Review GPT-5.6 §7): läuft nur bei gestopptem Server (Port-
Check; --force nur, wenn der laufende Server bereits den flock-Guard hat), unter
board_write_guard, mit Sidecar-Verifikation VOR dem Schrumpfen jeder Zeile,
Roundtrip-Check (lost-Guards + Turn-Zahl) VOR dem Schreiben, atomarem Write.
Idempotent: Turns mit vorhandenem Verweis werden übersprungen.

Nutzung:  python3 migrate_diet.py [--dry-run] [--force]
Exit 0 = ok, Exit 1 = Abbruch (nichts geschrieben).
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sidecar  # noqa: E402
from server import _all_items, board_write_guard, lost_total, parse_board, serialize_board  # noqa: E402

import paths as _p  # noqa: E402

BOARD = _p.BOARD
PORT = 47822


def server_running() -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _thread_count(board: dict) -> int:
    return sum(len(it.get("thread", [])) for _s, _n, _c, it in _all_items(board))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="trotz laufendem Server (NUR wenn der bereits den flock-Guard hat)")
    ap.add_argument("--file", type=Path, default=BOARD, help="Board-Datei (Tests); Default = live board.md")
    args = ap.parse_args()
    board_file = args.file.resolve()

    if board_file == BOARD and server_running() and not args.force:
        print(f"migrate_diet: ABBRUCH — Board-Server läuft (Port {PORT}). "
              "Erst stoppen (run-diet-migration.sh macht die ganze Choreografie).")
        return 1

    with board_write_guard(board_file):
        text = board_file.read_text()
        board = parse_board(text)
        if lost_total(text, board) > 0:
            print("migrate_diet: ABBRUCH — board.md hat ungeparste Zeilen, nichts angefasst")
            return 1
        turns_before = _thread_count(board)

        moved = 0
        for _s, _n, _c, it in _all_items(board):
            for ev in it.get("thread", []):
                t = ev.get("text", "")
                if (ev.get("kind") not in ("ask", "reply") or len(t) <= sidecar.INLINE_MAX
                        or sidecar.REF_RE.search(t)):
                    continue
                if args.dry_run:
                    moved += 1
                    continue
                new = sidecar.inline_turn(it.get("id") or "item", it.get("title", ""), t,
                                          kind=ev["kind"])
                # Verifikation VOR dem Schrumpfen: der Volltext muss wirklich im Sidecar liegen.
                m = sidecar.REF_RE.search(new)
                if not m or t.strip() not in (sidecar.SIDECAR_DIR / m.group(1)).read_text():
                    print(f"migrate_diet: ABBRUCH — Sidecar-Verifikation fehlgeschlagen "
                          f"(Item {it.get('id')}), nichts geschrieben")
                    return 1
                ev["text"] = new
                moved += 1

        if args.dry_run:
            print(f"migrate_diet (dry-run): würde {moved} Turn(s) auslagern "
                  f"(board.md aktuell {len(text) // 1024} KB)")
            return 0
        if not moved:
            print("migrate_diet: nichts zu migrieren — alle Turns unter der Schwelle")
            return 0

        out = serialize_board(board)
        re_board = parse_board(out)
        if lost_total(out, re_board) > 0 or _thread_count(re_board) != turns_before:
            print("migrate_diet: ABBRUCH — Roundtrip-Check fehlgeschlagen, nichts geschrieben")
            return 1
        tmp = board_file.with_name(".board-migrate.tmp")
        tmp.write_text(out)
        tmp.replace(board_file)
        print(f"migrate_diet: OK — {moved} Turn(s) ausgelagert, "
              f"{len(text) // 1024} KB → {len(out) // 1024} KB")
        return 0


if __name__ == "__main__":
    sys.exit(main())
