"""Stolperdraht fuer die Bildsprache des Boards.

Warum es diesen Test gibt: die Umstellung der Vollfarb-Emoji auf die eigene
1-Bit-Bildsprache war einmal zu rund zwei Dritteln erledigt und blieb dort liegen —
nichts im Repo hielt sie fest, also brachte jede neue Action und jeder neue
Hinweistext wieder ein Farb-Emoji mit. Genau das faengt dieser Test ab: er prueft
die Regel, nicht den Einzelfall.

Zwei Invarianten:
  1. Jede Quick Action in actions.json hat ein 16x16-Sprite in ACTION_MARKS.
  2. Kein Vollfarb-Emoji steht in index.html, ausser es hat einen Glyph in
     EMOJI_GLYPH oder es steht begruendet auf der Allowlist unten.

Monochrome Typografie (→ ✓ ⚠ ▶ ★ ●) ist ausdruecklich KEIN Fall fuer diesen Test:
sie kommt aus der Textschrift und war nie gemeint. Geprueft wird die Pictograph-Ebene
(U+1F300–U+1FAFF) plus die wenigen BMP-Zeichen, die Systeme von sich aus farbig malen.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

# Zeichen, die Systeme ohne Variantenselektor farbig rendern und die deshalb aus der
# 1-Bit-Welt herausfallen. Bewusst kurz gehalten: lieber ein paar Faelle zu wenig als
# monochrome Typografie mitzufangen und den Test unbrauchbar zu machen.
EMOJI = re.compile("[\U0001F300-\U0001FAFF⌚⌛⏰⏱⏳✅❌❓⚡]")

# Begruendete Ausnahmen. Jede Zeile braucht einen Grund, sonst ist es keine Ausnahme,
# sondern eine vergessene Aufgabe.
ALLOWLIST: set[str] = set()


def _strip_comments(src: str) -> str:
    """Kommentare raus — was nur im Quelltext steht, sieht niemand in der UI.

    Grob, aber in die sichere Richtung: `//` in einem String (etwa `http://`) kappt den
    Rest der Zeile, der Test uebersieht dann eher etwas, statt falsch Alarm zu schlagen.
    """
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _mapped_emoji() -> set[str]:
    block = re.search(r"const EMOJI_GLYPH = \{(.*?)\n\};", HTML, re.S).group(1)
    return set(re.findall(r'"([^"]{1,3})":\s*"[a-z]+"', block))


def test_every_quick_action_has_a_sprite():
    block = re.search(r"const ACTION_MARKS = \{(.*?)\n\};", HTML, re.S).group(1)
    sprites = set(re.findall(r'^\s*"([a-z0-9\-_]+)":\[', block, re.M))
    keys = [a["key"] for a in json.loads((ROOT / "actions.json").read_text(encoding="utf-8"))["actions"]]
    missing = [k for k in keys if k not in sprites]
    assert not missing, (
        f"Quick Actions ohne 16x16-Sprite in ACTION_MARKS: {missing}. "
        "Ohne Sprite faellt die Karte auf das Farb-Emoji aus actions.json zurueck.")


def test_sprites_are_16x16_and_use_the_known_alphabet():
    block = re.search(r"const ACTION_MARKS = \{(.*?)\n\};", HTML, re.S).group(1)
    for m in re.finditer(r'"([a-z0-9\-_]+)":\[(.*?)\]', block, re.S):
        rows = re.findall(r'"([^"]*)"', m.group(2))
        assert len(rows) == 16, f"{m.group(1)}: {len(rows)} Zeilen statt 16"
        assert all(len(r) == 16 for r in rows), f"{m.group(1)}: Zeile nicht 16 Zeichen breit"
        assert set("".join(rows)) <= set(".#ob a"), f"{m.group(1)}: unbekannte Zelle"


def test_pixglyph_entries_are_8x8():
    block = re.search(r"const PIXGLYPH = \{(.*?)\n\};", HTML, re.S).group(1)
    for m in re.finditer(r'^\s*([a-z]+):\s*\[(.*?)\],', block, re.S | re.M):
        rows = re.findall(r'"([^"]*)"', m.group(2))
        assert len(rows) == 8 and all(len(r) == 8 for r in rows), f"{m.group(1)}: kein 8x8"


def test_every_emoji_glyph_target_exists():
    block = re.search(r"const PIXGLYPH = \{(.*?)\n\};", HTML, re.S).group(1)
    names = set(re.findall(r"^\s*([a-z]+):\s*\[", block, re.M))
    block2 = re.search(r"const EMOJI_GLYPH = \{(.*?)\n\};", HTML, re.S).group(1)
    for emoji, name in re.findall(r'"([^"]{1,3})":\s*"([a-z]+)"', block2):
        assert name in names, f"{emoji} zeigt auf Glyph '{name}', den es nicht gibt"


def test_no_unmapped_colour_emoji_reaches_the_ui():
    mapped = _mapped_emoji()
    offenders: dict[str, list[int]] = {}
    for lineno, line in enumerate(_strip_comments(HTML).splitlines(), 1):
        for ch in EMOJI.findall(line):
            if ch in mapped or ch in ALLOWLIST:
                continue
            offenders.setdefault(ch, []).append(lineno)
    assert not offenders, (
        "Vollfarb-Emoji ohne Glyph in index.html: "
        + ", ".join(f"{ch} (Zeile {ls[0]})" for ch, ls in offenders.items())
        + ". Entweder einen 8x8-Glyph in PIXGLYPH anlegen und in EMOJI_GLYPH verdrahten, "
          "oder — in Tooltips und Hilfetexten — das Zeichen durch Worte ersetzen.")
