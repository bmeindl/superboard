"""Stolperdraht für die Icon-Pipeline (make-icon.py).

Warum: Das Dock-/Finder-Icon wird aus DER Markenmatrix in index.html gerechnet, damit
Kopfzeile und Icon nicht auseinanderdriften. Bis 22.08. rechnete `write_icns` die kleinen
Größen per NEAREST aus dem 512er herunter — bei 24-px-Kästchen und 32-px-Abtastschritt
fiel jede dritte Bitmap-Spalte weg, der Rahmen verschwand auf zwei Seiten. Sichtbar war
das nur, wenn man das Icon bei 16 px tatsächlich ANSCHAUT; kein Test hat es gehalten.
Dieser hier hält es: jede ausgelieferte Größe muss die Matrix exakt wiedergeben.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("make_icon", ROOT / "make-icon.py")
mi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mi)

# Alle Größen, die in icon.icns landen (Basis + @2x) plus das ausgelieferte icon.png.
ICNS_SIZES = [s for base in (16, 32, 128, 256, 512) for s in (base, base * 2)]


def test_cell_and_frame_stay_integral():
    """Ein Bitmap kennt nur volle Kästchen — jede Größe braucht eine ganzzahlige Zelle."""
    for size in ICNS_SIZES:
        assert mi.cell_px(size) >= 1, f"{size}px: Zelle unter 1 px, Zeichen kollabiert"
        assert mi.frame_px(size) >= 1, f"{size}px: Rahmen unter 1 px"
    # Die 512er-Geometrie ist die abgenommene (früher INNER=384 / FRAME=16) — nicht driften.
    assert mi.cell_px(512) == 24
    assert mi.frame_px(512) == 16


@pytest.mark.parametrize("size", ICNS_SIZES)
def test_every_size_reproduces_the_matrix_exactly(size: int):
    """Jedes Amber-Pixel gehört zu einer `#`-Zelle und jede `#`-Zelle ist voll gefüllt."""
    rows = mi.read_mark()
    img = mi.render(rows, size)
    px = mi.cell_px(size)
    used_x = [x for x in range(16) if any(r[x] == "#" for r in rows)]
    used_y = [y for y, r in enumerate(rows) if "#" in r]
    off_x = (size - (used_x[-1] - used_x[0] + 1) * px) // 2 - used_x[0] * px
    off_y = (size - (used_y[-1] - used_y[0] + 1) * px) // 2 - used_y[0] * px

    expected = {
        (off_x + x * px + dx, off_y + y * px + dy)
        for y, row in enumerate(rows)
        for x, ch in enumerate(row)
        if ch == "#"
        for dx in range(px)
        for dy in range(px)
    }
    actual = {
        (x, y)
        for x in range(size)
        for y in range(size)
        if img.getpixel((x, y)) == mi.AMBER
    }
    assert actual == expected, f"{size}px: Marke weicht von der Matrix ab"


@pytest.mark.parametrize("size", ICNS_SIZES)
def test_frame_survives_on_all_four_edges(size: int):
    """Der Cabinet-Rahmen war der erste, der beim Herunterrechnen wegbrach."""
    img = mi.render(mi.read_mark(), size)
    last = size - 1
    corners = [(0, 0), (last, 0), (0, last), (last, last)]
    mids = [(size // 2, 0), (size // 2, last), (0, size // 2), (last, size // 2)]
    for pos in corners + mids:
        assert img.getpixel(pos) == mi.BOARD_LINE, f"{size}px: Rahmen fehlt bei {pos}"


def test_glyph_keeps_its_letter_proportion():
    """Die Marke ist ein S, kein Block: 12 Spalten × 11 Zeilen (Variante „A · Kante",
    22.08. bestätigt — die breite Fassung mit 14 Spalten war die verworfene Alternative)."""
    rows = mi.read_mark()
    used_x = [x for x in range(16) if any(r[x] == "#" for r in rows)]
    used_y = [y for y, r in enumerate(rows) if "#" in r]
    assert len(used_x) == 12 and len(used_y) == 11
