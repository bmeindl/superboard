#!/usr/bin/env python3
"""Erzeugt icon.png (Dock-/Touch-Icon des Boards) aus der Wortmarke in index.html.

Warum ein Generator statt einer gepflegten PNG-Datei: die Marke ist ein 16×16-Bitmap,
das im Frontend als `BRAND_MARK` lebt (Kopfzeile links vom Titel). Zwei Kopien derselben
Pixel driften garantiert auseinander — also wird das Icon aus DER Matrix gerechnet.

    python3 make-icon.py          # schreibt icon.png (512×512)

Der Server liefert icon.png unter /apple-touch-icon.png aus (s. server.py).
Wortmarke: Bitmap-Monogramm in Amber #F2B23E, statisch, links vor dem Titel + Dock-Icon.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
SIZE = 512          # Dock/Retina-tauglich
AMBER = (242, 178, 62)
BOARD = (15, 16, 36)        # --board
BOARD_LINE = (66, 70, 115)  # --board-line, der Cabinet-Rahmen
INK_SHARE = 0.75            # Anteil der Kantenlänge, den die Marke einnimmt (512 → 384)


def cell_px(size: int) -> int:
    """Kantenlänge EINES Bitmap-Pixels bei Icon-Größe `size` — ganzzahlig, immer ≥ 1.

    Ganzzahlig ist die ganze Pointe: ein Bitmap kennt nur volle Kästchen. Bei 512
    ergibt das die alten 24 px (16 Zellen × 24 = 384 = INK_SHARE), bei 32 px sind es
    2 px je Zelle, bei 16 px genau 1.
    """
    return max(1, round(size * INK_SHARE / 16))


def frame_px(size: int) -> int:
    """Rahmenstärke, proportional zur Icon-Größe (512 → 16 px), aber nie unter 1."""
    return max(1, round(size / 32))


def read_mark() -> list[str]:
    """`const BRAND_MARK=[...]` aus index.html ziehen — eine Quelle für Kopfzeile + Icon."""
    src = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r"const BRAND_MARK=\[(.*?)\];", src, re.S)
    if not m:
        sys.exit("BRAND_MARK in index.html nicht gefunden — Marke umbenannt?")
    rows = re.findall(r'"([^"]*)"', m.group(1))
    if len(rows) != 16 or any(len(r) != 16 for r in rows):
        sys.exit(f"BRAND_MARK ist nicht 16×16 (gefunden: {len(rows)} Zeilen)")
    return rows


def render(rows: list[str], size: int = SIZE) -> Image.Image:
    """Zentriert wird die GLYPHE, nicht die Matrix: im 16er-Raster hat die Marke oben
    und unten unterschiedlich viele Leerzeilen (sie ist auf die Versalhöhe von „Board"
    getrimmt, nicht auf Symmetrie). Würde man stumpf die Matrix mittig setzen, erbte
    das Dock-Icon diese Schieflage."""
    img = Image.new("RGB", (size, size), BOARD)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size - 1, size - 1], outline=BOARD_LINE, width=frame_px(size))
    px = cell_px(size)
    used_y = [y for y, row in enumerate(rows) if "#" in row]
    used_x = [x for x in range(16) if any(row[x] == "#" for row in rows)]
    off_x = (size - (used_x[-1] - used_x[0] + 1) * px) // 2 - used_x[0] * px
    off_y = (size - (used_y[-1] - used_y[0] + 1) * px) // 2 - used_y[0] * px
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "#":
                d.rectangle([off_x + x * px, off_y + y * px,
                             off_x + (x + 1) * px - 1, off_y + (y + 1) * px - 1], fill=AMBER)
    return img


def write_icns(rows: list[str]) -> Path | None:
    """Zusätzlich icon.icns für den Dock-Fall: macOS bäckt das Icon einer Safari-Web-App
    EINMAL beim „Zum Dock hinzufügen" ins Bundle (~/Applications/Board.app) und schaut
    danach nie wieder auf den Server. Wer die Marke im Dock tauschen will, ohne die
    Web-App neu anzulegen, braucht diese Datei. Ohne `iconutil` (Nicht-macOS) überspringen.

    JEDE Größe wird aus der Matrix neu gerechnet, nicht aus dem 512er heruntergerechnet.
    Warum (nachgemessen 22.08.): 512 → NEAREST → 16 tastet alle 32 px ab, die Kästchen
    sind aber 24 px breit — dabei fällt jede dritte Bitmap-Spalte weg und der Rahmen
    verschwindet auf zwei Seiten. Das Zeichen kam in Finder-Liste, Menüleiste und
    kleinem Dock zerfranst an. Aus der Matrix gerechnet ist jede Größe wieder exakt."""
    import shutil
    import subprocess
    if not shutil.which("iconutil"):
        return None
    iconset = ROOT / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for base in (16, 32, 128, 256, 512):
        render(rows, base).save(iconset / f"icon_{base}x{base}.png")
        render(rows, base * 2).save(iconset / f"icon_{base}x{base}@2x.png")
    out = ROOT / "icon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(iconset)
    return out


def main() -> int:
    rows = read_mark()
    img = render(rows)
    out = ROOT / "icon.png"
    img.save(out)
    print(f"{out.relative_to(ROOT.parents[1])} geschrieben ({out.stat().st_size} Bytes, {SIZE}×{SIZE})")
    icns = write_icns(rows)
    if icns:
        print(f"{icns.relative_to(ROOT.parents[1])} geschrieben ({icns.stat().st_size} Bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
