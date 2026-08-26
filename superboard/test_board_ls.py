"""Tests für board_ls.py — die Board-Schnellübersicht für Agenten-Kontexte.

Die eine Invariante, die zählt: **Triage-Prompt und Schnellübersicht zeigen dieselbe
Zeile.** Beide behaupten dasselbe Board; zwei Formatierer würden auseinanderlaufen und
dabei zwei verschiedene Boards erzählen (ARCHITEKTUR-Invariante 16). Der Test hält die
gemeinsame Quelle `item_row` fest — nicht ihr genaues Format, das darf sich ändern.

Dazu die beiden Eigenschaften, wegen denen das Tool überhaupt existiert: der Filter darf
nichts still verschlucken, und das Archiv muss mitgesucht werden können (die Antwort auf
„gibt es das schon?" ist oft „ja, im Mai erledigt").
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_ls  # noqa: E402
import server  # noqa: E402

BOARD = """# Board

## Thema A

### Jetzt

- [ ] Erstes Item *(2026-07-01)*
  Kontext zum ersten Item mit WhatsApp drin.
  @gc-id: aaaabbbbcccc

- [x] Erledigtes Item *(2026-07-02)*
  @gc-id: ddddeeeeffff

### Bald

- [ ] Zweites Item *(2026-07-03)*
  @gc-id: 111122223333

# Personen

## Nova → personal/people/nova.md

- [ ] Personen-Item *(2026-07-04)*
  @gc-id: 444455556666
"""

ARCHIVE = """# Board-Archiv

## 2026-07-10

- [x] Altes WhatsApp-Thema *(2026-07-09)* ← Thema A / Jetzt
  Damals schon einmal durchgekaut.
  @gc-done:

## 2026-07-12

- [x] Ganz anderes Ding *(2026-07-11)* ← Person: Nova
"""


def test_item_row_ist_die_gemeinsame_quelle_mit_der_triage():
    """Jede Zeile des Triage-Prompts muss aus item_row stammen — sonst driften die
    beiden Darstellungen desselben Boards auseinander."""
    board = server.parse_board(BOARD)
    today = date(2026, 7, 5)
    prompt_zeilen = [
        z for z in server._triage_prompt(board, today).split("\n") if z.startswith("- id=")
    ]
    eigene = [
        server.item_row(s, n, c, it, today)
        for s, n, c, it in server._all_items(board)
        if not it["done"] and s != "cockpit"
    ]
    assert prompt_zeilen == eigene
    assert len(eigene) == 3  # das erledigte Item ist in der Triage bewusst nicht dabei


def test_item_row_traegt_ort_alter_und_kontextanriss():
    board = server.parse_board(BOARD)
    s, n, c, it = next(iter(server._all_items(board)))
    zeile = server.item_row(s, n, c, it, date(2026, 7, 5))
    assert "id=aaaabbbbcccc" in zeile
    assert "[Thema A/Jetzt]" in zeile
    assert "Erstes Item" in zeile
    assert "open 4d" in zeile
    assert "Kontext zum ersten Item" in zeile


def test_filter_greift_auf_body_und_verlangt_alle_woerter():
    board = server.parse_board(BOARD)
    _, n, c, it = next(iter(server._all_items(board)))
    hay = board_ls.haystack(n, c, it)
    assert "whatsapp" in hay          # Treffer aus dem BODY, nicht nur dem Titel
    assert "thema a" in hay           # Thema zählt mit
    assert "aaaabbbbcccc" in hay      # und die id
    assert "zweites" not in hay


def test_archiv_wird_als_eigenes_format_gelesen():
    """Das Archiv ist kein Board (Datums-Überschriften, Herkunft hinter dem Titel) —
    es braucht den eigenen Parser, sonst fällt die halbe Historie stumm raus."""
    rows = board_ls.archive_rows(ARCHIVE)
    assert len(rows) == 2
    zeile, hay = rows[0]
    assert "ARCHIV · 2026-07-09" in zeile
    assert "[Thema A / Jetzt]" in zeile
    assert "Altes WhatsApp-Thema" in zeile
    assert "Damals schon einmal durchgekaut" in zeile
    assert "whatsapp" in hay


def test_cli_laeuft_read_only_und_filtert(tmp_path):
    board_datei = tmp_path / "board.md"
    board_datei.write_text(BOARD)
    vorher = board_datei.read_text()

    def lauf(*args):
        p = subprocess.run(
            [sys.executable, str(HERE / "board_ls.py"), "--file", str(board_datei), *args],
            capture_output=True, text=True, check=True,
        )
        return p.stdout

    offen = lauf()
    assert "Erstes Item" in offen and "Erledigtes Item" not in offen
    assert "Personen-Item" in offen          # Personen-Items gehören dazu
    assert "Erledigtes Item" in lauf("--all")
    treffer = lauf("--grep", "whatsapp")
    assert "Erstes Item" in treffer and "Zweites Item" not in treffer
    assert "keine Treffer" in lauf("--grep", "zzznix")

    assert board_datei.read_text() == vorher  # read-only, immer
