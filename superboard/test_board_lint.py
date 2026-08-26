"""Tests für board_lint.py — die Sperr-Diagnose des Boards.

Die eine Invariante, die wirklich zählt: **Lint und Server-Guard müssen sich immer
einig sein.** Der Guard (`server.lost_total`) entscheidet, ob das Board schreibbar ist;
der Lint erklärt, warum nicht. Driften die beiden auseinander, zeigt der Lint entweder
Zeilen, die gar nicht schuld sind (Fehlalarm, schickt die Suche in die falsche Ecke —
genau der Burn vom 28.07.), oder er verschweigt die schuldige Zeile.

Beide messen bewusst UNTERSCHIEDLICH: der Guard zählt Regex-Treffer gegen geparste
Felder, der Lint stellt den Round-Trip parse→serialize nach. Dass zwei unabhängige
Verfahren dieselbe Zahl liefern, ist der Wert dieser Tests — eine gemeinsame Hilfs-
funktion würde genau das wegtesten.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_lint  # noqa: E402


def _server():
    spec = importlib.util.spec_from_file_location("_srv_for_lint", HERE / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SRV = _server()

# Minimalboard in der Form, die parse_board erwartet — bewusst klein und hier im Test
# sichtbar, statt die echte board.md zu laden: die ändert sich stündlich, und ein Test,
# der an fremdem Live-Zustand hängt, wird irgendwann aus dem falschen Grund rot.
CLEAN = """# Board

## Dev

### Jetzt

- [ ] Ein Item *(2026-07-28)*
  @gc-id: aaaabbbbcccc
  @gc: eine Frage
  @gc-re: eine Antwort
  @gc-session: 11111111-2222-3333-4444-555555555555 · board-ein-item

- [ ] Zweites Item *(2026-07-28)*
  @gc-id: ddddeeeeffff

### Bald

# Notizen

Freitext hier unten zählt nicht mit.
"""

# Eine Checkbox, die der Parser WIRKLICH verliert: eingerückt, aber ohne Item darüber,
# an das sie sich als Sub hängen könnte. (Zwischen zwei Items eingerückt wäre sie
# schlicht ein Sub — kein Verlust, und der Guard sagt dort korrekt 0.)
KAPUTTE_BOX = CLEAN.replace("### Bald\n", "### Bald\n\n   - [ ] schief eingerueckt\n")


def _lint(text):
    return board_lint.lint(text, server=SRV)


def test_sauberes_board_ist_offen():
    r = _lint(CLEAN)
    assert r["locked"] is False
    assert r["lost"] == 0
    assert r["lines"] == []
    assert r["items"] == 2


def test_round_trip_verliert_nichts():
    """Grundlage des Verfahrens: serialize(parse(x)) verliert auf einem sauberen Board
    keine Zeile. Bricht das, produziert der Lint Fehlalarme — dann gehört
    board_lint._norm() nachgezogen, nicht der Befund weggedrückt.

    Bewusst nur die VERLUST-Richtung: serialize ergänzt fehlendes Gerüst (leere Spalten,
    '# Personen'). Zeilen dazuzuerfinden ist harmlos — vernichtete Zeilen sind der Schaden."""
    board = SRV.parse_board(CLEAN)
    verloren = board_lint._norm(CLEAN, SRV) - board_lint._norm(SRV.serialize_board(board), SRV)
    assert not verloren, f"Round-Trip verliert: {dict(verloren)}"


@pytest.mark.parametrize("name,text,erwartet_in_zeile", [
    ("doppelte @gc-id",
     CLEAN.replace("  @gc-id: aaaabbbbcccc\n",
                   "  @gc-id: aaaabbbbcccc\n  @gc-id: 999999999999\n"),
     "@gc-id:"),
    ("verwaister Faden-Turn",
     CLEAN.replace("### Jetzt\n", "### Jetzt\n  @gc: turn ohne item\n"),
     "@gc:"),
    ("kaputt eingerückte Checkbox", KAPUTTE_BOX, "- [ ]"),
])
def test_defekt_wird_gefunden(name, text, erwartet_in_zeile):
    r = _lint(text)
    assert r["locked"] is True, f"{name}: Lint hält das Board für sauber"
    assert r["lines"], f"{name}: gesperrt, aber keine Zeile benannt"
    assert any(erwartet_in_zeile in e["text"] for e in r["lines"]), \
        f"{name}: falsche Zeile benannt — {[e['text'] for e in r['lines']]}"
    assert all(e["line"] >= 1 and e["hint"] for e in r["lines"]), \
        f"{name}: Zeilennummer oder Erklärung fehlt"


@pytest.mark.parametrize("name,text", [
    ("sauber", CLEAN),
    ("doppelte @gc-id",
     CLEAN.replace("  @gc-id: aaaabbbbcccc\n", "  @gc-id: aaaabbbbcccc\n  @gc-id: 999999999999\n")),
    ("verwaister Turn", CLEAN.replace("### Jetzt\n", "### Jetzt\n  @gc: turn ohne item\n")),
    ("kaputte Box", KAPUTTE_BOX),
    ("Box zwischen Items (= Sub, kein Verlust)",
     CLEAN.replace("- [ ] Zweites Item", "   - [ ] wird ein Sub\n- [ ] Zweites Item")),
    ("doppelte @gc-session",
     CLEAN.replace("  @gc-session: 11111111-2222-3333-4444-555555555555 · board-ein-item\n",
                   "  @gc-session: 11111111-2222-3333-4444-555555555555 · a\n"
                   "  @gc-session: 66666666-7777-8888-9999-000000000000 · b\n")),
    ("Zeile im Header", CLEAN.replace("# Board\n", "# Board\n- [ ] to-do ueber der ersten ueberschrift\n")),
    ("Zeile in # Notizen", CLEAN.replace("Freitext hier unten", "- [ ] box in notizen\nFreitext hier unten")),
])
def test_lint_und_guard_sind_sich_einig(name, text):
    """Die Invariante. Der Guard sperrt, der Lint erklärt — beide über dieselbe Menge.

    Bewusst mit den Nicht-Verlust-Fällen (Sub, Header, Notizen) gemischt: eine Diagnose,
    die auch dort „gesperrt" ruft, ist schlimmer als keine — sie schickt die Suche auf
    eine Zeile, die gar nicht schuld ist."""
    r = _lint(text)
    guard = SRV.lost_total(text, SRV.parse_board(text))
    assert r["lost"] == guard, f"{name}: Lint sagt {r['lost']}, Guard sagt {guard}"
    assert r["locked"] == (guard > 0)


def test_header_zeile_sperrt_das_board_nicht():
    """Regression (28.07.): eine Checkbox über der ersten Überschrift zählte als
    verlorene Zeile und sperrte damit JEDEN Schreibpfad — obwohl serialize_board den
    Header wörtlich zurückschreibt. Fehlalarm mit maximalem Schaden, siehe guard_scope."""
    text = CLEAN.replace("# Board\n", "# Board\n- [ ] to-do ueber der ersten ueberschrift\n")
    erhalten = "- [ ] to-do ueber der ersten ueberschrift" in SRV.serialize_board(SRV.parse_board(text))
    assert erhalten, "Annahme gebrochen: Header wird NICHT mehr verbatim durchgereicht"
    assert SRV.lost_total(text, SRV.parse_board(text)) == 0, "Guard sperrt für eine erhaltene Zeile"
    assert _lint(text)["locked"] is False


def test_checkbox_zeigt_das_item_darueber_nicht_sich_selbst():
    """Regression: `_owner` startete bei der Zeile selbst — eine kaputte Checkbox war
    damit ihr eigener Besitzer ("am Item: schief eingerueckt"), was nichts erklärt."""
    box = next(e for e in _lint(KAPUTTE_BOX)["lines"] if "- [ ]" in e["text"])
    assert "schief eingerueckt" not in box["item"], "Zeile ist ihr eigener Besitzer"
    assert "Zweites Item" in box["item"], f"falscher Besitzer: {box['item']!r}"


def test_exit_code_signalisiert_die_sperre(tmp_path, capsys):
    """Der CLI ist auch für Skripte/Health-Checks gedacht — Exit 1 = gesperrt."""
    p = tmp_path / "board.md"
    p.write_text(CLEAN)
    argv = sys.argv
    try:
        sys.argv = ["board_lint.py", str(p)]
        assert board_lint.main() == 0
        p.write_text(CLEAN.replace("### Jetzt\n", "### Jetzt\n  @gc: turn ohne item\n"))
        assert board_lint.main() == 1
        assert "GESPERRT" in capsys.readouterr().out
    finally:
        sys.argv = argv


# --- Struktur-Doppel -----------------------------------------------------------
# Zweiter Befundtyp neben dem Round-Trip: Zeilen, die es DOPPELT gibt. Ein Splice-
# Unfall kann Items und eine `##`-Überschrift verdoppeln, und `lost` bleibt dabei 0 —
# doppelte Items sind syntaktisch völlig legal. Genau deshalb braucht es eine eigene
# Prüfung; der Round-Trip kann sie strukturell nicht sehen.

DOPPELTES_ITEM = CLEAN.replace(
    "- [ ] Zweites Item *(2026-07-28)*\n  @gc-id: ddddeeeeffff\n",
    "- [ ] Zweites Item *(2026-07-28)*\n  @gc-id: ddddeeeeffff\n\n"
    "- [ ] Zweites Item *(2026-07-28)*\n  @gc-id: ddddeeeeffff\n")

DOPPELTES_THEMA = CLEAN.replace("### Bald\n", "### Bald\n\n## Dev\n\n### Jetzt\n")


def test_sauberes_board_hat_keine_doppel():
    r = _lint(CLEAN)
    assert r["dup_ids"] == []
    assert r["dup_themes"] == []


def test_doppelte_gc_id_wird_gefunden_sperrt_aber_nicht():
    """Das Board darf beschreibbar bleiben: ein Doppel ist ein Datenfehler, kein Grund,
    den Menschen mit 409 auszusperren. Sichtbar sein muss es trotzdem."""
    r = _lint(DOPPELTES_ITEM)
    assert r["locked"] is False, "Doppel darf keinen Schreibpfad sperren"
    assert r["lost"] == 0
    assert [d["id"] for d in r["dup_ids"]] == ["ddddeeeeffff"]
    d = r["dup_ids"][0]
    assert d["count"] == 2
    assert len(d["lines"]) == 2, f"beide Fundstellen benennen, nicht {d['lines']}"
    assert all("Zweites Item" in t for t in d["titles"])


def test_doppelte_themen_ueberschrift_wird_gefunden():
    r = _lint(DOPPELTES_THEMA)
    assert r["locked"] is False
    assert [d["name"] for d in r["dup_themes"]] == ["Dev"]
    assert r["dup_themes"][0]["count"] == 2
    assert len(r["dup_themes"][0]["lines"]) == 2


def test_gc_id_im_fliesstext_ist_kein_doppel():
    """Regression aus dem echten Board: ein Body verweist per `@gc-id: …` im Fließtext
    auf ein anderes Item (Cross-Ref). Eine Textsuche würde daraus ein Doppel machen —
    gezählt wird deshalb über die geparsten Items, nicht über den Rohtext."""
    text = CLEAN.replace(
        "- [ ] Zweites Item *(2026-07-28)*\n",
        "- [ ] Zweites Item *(2026-07-28)*\n"
        "  Cross-Ref: gehört zum selben Strang wie `@gc-id: aaaabbbbcccc` oben.\n")
    r = _lint(text)
    assert r["dup_ids"] == [], f"Cross-Ref falsch als Doppel gezählt: {r['dup_ids']}"


def test_exit_code_meldet_auch_struktur_doppel(tmp_path, capsys):
    """Der Exit-Code ist die einzige Stelle, an der ein Skript den Befund mitbekommt."""
    p = tmp_path / "board.md"
    argv = sys.argv
    try:
        sys.argv = ["board_lint.py", str(p)]
        p.write_text(DOPPELTES_ITEM)
        assert board_lint.main() == 1
        out = capsys.readouterr().out
        assert "STRUKTUR" in out and "ddddeeeeffff" in out
        assert "OK —" in out, "unsperrt: der OK-Kopf muss trotzdem stehen"
    finally:
        sys.argv = argv
