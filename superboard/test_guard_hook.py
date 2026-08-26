"""Tests für guard_hook.py — den PostToolUse-Riegel gegen Struktur-Doppel.

Der Hook wird hier bewusst als SUBPROZESS mit JSON auf stdin gefahren, nicht per
Funktionsaufruf: genau so ruft Claude Code ihn auf, und die halbe Wirkung steckt im
Exit-Code (2 = stderr geht zurück ins Modell). Ein Import-Test würde die eine
Eigenschaft nicht prüfen, auf die es ankommt.

`TMPDIR` zeigt in jedem Test auf `tmp_path` — der Hook legt seinen Session-Stempel
dort ab, und ein Test darf den Stempel einer laufenden echten Session nicht anfassen.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "guard_hook.py"

CLEAN = """# Board

## Dev

### Jetzt

- [ ] Ein Item *(2026-08-21)*
  @gc-id: aaaabbbbcccc
  @gc: eine Frage

- [ ] Zweites Item *(2026-08-21)*
  @gc-id: ddddeeeeffff

### Bald

# Notizen
"""

# Der Splice-Unfall in klein: die Region zwischen den Ankern steht zweimal, samt
# Themen-Überschrift. Syntaktisch völlig legal — lost_total bleibt 0, genau darum
# war der Round-Trip-Guard blind dafür.
DOPPELT = CLEAN.replace("# Notizen\n", """## Dev

### Jetzt

- [ ] Ein Item *(2026-08-21)*
  @gc-id: aaaabbbbcccc
  @gc: eine Frage

- [ ] Zweites Item *(2026-08-21)*
  @gc-id: ddddeeeeffff

### Bald

# Notizen
""")


def _run(board: Path, tmp: Path, session: str = "test", **env_extra):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp),
           "TMPDIR": str(tmp), "GC_BOARD": str(board), **env_extra}
    return subprocess.run([sys.executable, str(HOOK)],
                          input=json.dumps({"session_id": session}),
                          capture_output=True, text=True, env=env)


def test_sauberes_board_schweigt(tmp_path):
    board = tmp_path / "board.md"
    board.write_text(CLEAN)
    r = _run(board, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == ""


def test_doppel_schlaegt_an_und_nennt_die_id(tmp_path):
    board = tmp_path / "board.md"
    board.write_text(DOPPELT)
    r = _run(board, tmp_path)
    assert r.returncode == 2, f"Doppel nicht gemeldet: {r.stdout!r} {r.stderr!r}"
    assert "aaaabbbbcccc" in r.stderr
    assert "board_lint" in r.stderr, "ohne Prüfbefehl kann der Agent nicht gegenzählen"


def test_meldung_bleibt_kurz(tmp_path):
    """Gegen einen Schaden wie 67 Doppel wären es ~90 Zeilen je Tool-Call gewesen.

    Die Meldung landet im Modellkontext, nicht auf einem Terminal — sie muss den
    Befund tragen, nicht den ganzen Report."""
    board = tmp_path / "board.md"
    viele = "".join(f"- [ ] Item {i} *(2026-08-21)*\n  @gc-id: aaaabbbb{i:04d}\n\n"
                    for i in range(30) for _ in range(2))
    board.write_text(CLEAN.replace("# Notizen\n", viele + "# Notizen\n"))
    r = _run(board, tmp_path)
    assert r.returncode == 2
    assert len(r.stderr.splitlines()) < 25, r.stderr
    assert "weitere" in r.stderr, "Kappung muss sagen, dass sie kappt"


def test_mtime_tor_und_noergel_grenze(tmp_path):
    """Unverändertes Board = kein Lauf. Stehendes Doppel = dreimal Warnung, dann Ruhe."""
    board = tmp_path / "board.md"
    board.write_text(CLEAN)
    assert _run(board, tmp_path, "s1").returncode == 0

    # Zweiter Aufruf auf unveränderter Datei: quittiert, still.
    assert _run(board, tmp_path, "s1").returncode == 0

    board.write_text(DOPPELT)
    codes = [_run(board, tmp_path, "s1").returncode for _ in range(5)]
    assert codes == [2, 2, 2, 0, 0], codes


def test_getrennte_stempel_je_session(tmp_path):
    """Eine parallele Session darf die Warnung nicht wegquittieren."""
    board = tmp_path / "board.md"
    board.write_text(DOPPELT)
    assert _run(board, tmp_path, "sessionA").returncode == 2
    assert _run(board, tmp_path, "sessionB").returncode == 2


def test_faellt_offen_aus(tmp_path):
    """Ein kaputter Wächter darf schweigen, aber niemals blockieren."""
    fehlt = tmp_path / "gibtsnicht.md"
    assert _run(fehlt, tmp_path).returncode == 0

    kein_file = tmp_path / "verzeichnis"
    kein_file.mkdir()
    assert _run(kein_file, tmp_path).returncode == 0

    board = tmp_path / "board.md"
    board.write_text(DOPPELT)
    r = subprocess.run([sys.executable, str(HOOK)], input="kein json",
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path),
                            "GC_BOARD": str(board)})
    assert r.returncode == 2, "kaputtes stdin darf den Befund nicht verschlucken"


def test_abschaltbar(tmp_path):
    board = tmp_path / "board.md"
    board.write_text(DOPPELT)
    assert _run(board, tmp_path, "s2", GC_BOARD_GUARD="off").returncode == 0
