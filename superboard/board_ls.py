#!/usr/bin/env python3
"""board_ls — Schnellübersicht des To-do-Boards für Agenten.

WARUM: `inbox/board.md` kann groß werden, weil jeder Agent-Faden inline im Item
lebt. Eine Session, die nur wissen will „gibt's dazu schon was?", sollte dafür
nicht den ganzen Verlauf in den Kontext laden. Dieser Reader gibt eine kompakte
Zeile pro Item aus.

    python3 -m superboard.board_ls                     # alle offenen Items
    python3 -m superboard.board_ls --grep project      # nur Treffer
    python3 -m superboard.board_ls --all               # Abgehaktes mit dazu
    python3 -m superboard.board_ls --grep mail --archive   # + inbox/board-archive.md

Findet --grep nichts, heißt das NICHT „gibt's nicht" — andere Wortwahl reicht schon.
Dann ohne --grep die ganze Liste holen und erst bei Bedarf die volle board.md lesen.

Read-only: fasst board.md nie an.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402  — parse_board + item_row, EINE Formatquelle mit der Triage

ARCHIVE = server.GC_ROOT / "inbox" / "board-archive.md"

# Das Archiv ist bewusst kein Board: `## <Datum>` statt Themen, Herkunft als „← Thema / Spalte"
# hinter dem Titel (sweep.py). parse_board() darauf loszulassen wäre Zufall — die paar Zeilen
# eigener Parser sind ehrlicher.
_ARCH_ITEM_RE = re.compile(r"^- \[[ xX]\] (.*?)(?: \*\((\d{4}-\d{2}-\d{2})\)\*)?(?: ← (.*))?$")


def archive_rows(text: str) -> list[tuple[str, str]]:
    """(Zeile, Suchtext) je archiviertem Item — gleiche Optik wie item_row."""
    rows: list[tuple[str, str]] = []
    title = herkunft = datum = ""
    body: list[str] = []

    def flush():
        if not title:
            return
        kurz = next((b for b in body if not b.startswith("@")), "")[:80]
        wann = f" · {datum}" if datum else ""
        rows.append((
            f"- [ARCHIV{wann}] [{herkunft or '?'}] {title}" + (f" · {kurz}" if kurz else ""),
            " ".join([title, herkunft, *body]).lower(),
        ))

    for line in text.split("\n"):
        m = _ARCH_ITEM_RE.match(line)
        if m:
            flush()
            title, datum, herkunft = m.group(1).strip(), m.group(2) or "", (m.group(3) or "").strip()
            body = []
        elif title and line.startswith("  ") and line.strip():
            body.append(line.strip())
        elif line.startswith("## "):
            flush()
            title, body = "", []
    flush()
    return rows


def haystack(n: str, c: str | None, it: dict) -> str:
    return " ".join([it.get("title", ""), n, c or "", it.get("id", ""), *(it.get("body") or [])]).lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grep", nargs="+", metavar="WORT",
                    help="nur Items, die ALLE Wörter enthalten (Titel, Body, Thema, id)")
    ap.add_argument("--all", action="store_true", help="abgehakte Items mit auflisten")
    ap.add_argument("--archive", action="store_true", help="zusätzlich inbox/board-archive.md durchsuchen")
    ap.add_argument("--file", default=str(server.DEFAULT_BOARD))
    args = ap.parse_args()

    terms = [t.lower() for t in (args.grep or [])]
    hit = lambda hay: all(t in hay for t in terms)  # noqa: E731

    board = server.parse_board(Path(args.file).read_text())
    today = date.today()

    rows, offen, gefiltert = [], 0, 0
    for s, n, c, it in server._all_items(board):
        if s == "cockpit":
            continue
        if not it["done"]:
            offen += 1
        if it["done"] and not args.all:
            continue
        if not hit(haystack(n, c, it)):
            gefiltert += 1
            continue
        rows.append(server.item_row(s, n, c, it, today))

    arch = [r for r, hay in archive_rows(ARCHIVE.read_text())if hit(hay)] if args.archive else []

    scope = "offene + abgehakte" if args.all else "offene"
    kopf = f"# Board ({scope} Items, {offen} offen gesamt"
    kopf += f" · Filter {' + '.join(terms)}: {len(rows)} Treffer, {gefiltert} raus" if terms else ""
    kopf += f" · Archiv: {len(arch)} Treffer" if args.archive else ""
    print(kopf + ")")
    print("\n".join(rows) if rows else "(keine Treffer im Board)")
    if args.archive:
        print("\n# Archiv (erledigt, inbox/board-archive.md)")
        print("\n".join(arch) if arch else "(keine Treffer im Archiv)")
    if terms and not rows and not arch:
        print("\n→ Kein Treffer heißt nicht „gibt es nicht“: ohne --grep die ganze Liste holen "
              "(~7,5k Token), sie ist billig genug.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
