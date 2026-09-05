#!/usr/bin/env python3
"""PostToolUse-Hook: meldet Struktur-Doppel in board.md dem Agenten, der sie gerade erzeugt hat.

## Warum ein Hook und nicht ein Guard im Server

Ein realer Schaden (67 Items und eine ganze `## Dev (Board)`-Überschrift
doppelt, 75 Minuten unbemerkt) ist NICHT über einen Server-Endpunkt entstanden,
sondern über einen Hand-Splice im Agenten: `s[:start] + neu + s[end:]` mit dem
End-Anker VOR dem Start-Anker dupliziert lautlos die ganze Region dazwischen.
Ein `board_write_guard` in `server.py` hätte davon nie etwas gesehen — der Server
war an diesem Schreibvorgang gar nicht beteiligt. Der Riegel muss deshalb dort
sitzen, wo geschrieben wird: am Werkzeug.

`board_lint.py` FINDET das Doppel seit demselben Tag, aber niemand ruft ihn auf.
Genau diese Lücke schließt der Hook: er läuft nach jedem Edit/Write/Bash, kostet
im Normalfall ~0 (siehe mtime-Tor unten) und gibt bei Exit 2 seinen stderr an das
Modell zurück. Der Agent, der das Doppel gebaut hat, sieht es Sekunden später im
eigenen Lauf — statt dass der Owner eine Stunde später Geisterkarten sieht.

## Drei Eigenschaften, die absichtlich so sind

* **mtime-Tor.** Der volle Lint kostet ~170 ms; das nach jedem Bash-Call zu zahlen
  wäre albern. Der Hook merkt sich pro Session die zuletzt gesehene mtime von
  board.md und tut gar nichts, solange die Datei unverändert ist. Bezahlt wird nur,
  wenn wirklich jemand geschrieben hat (auch der Server — das ist gewollt, so
  bemerkt eine fremde Session ein Doppel mit).
* **Fail-open.** JEDER interne Fehler endet in Exit 0. Ein kaputter Wächter darf
  niemals die Arbeit blockieren; er darf nur schweigen. Abschaltbar mit
  `GC_BOARD_GUARD=off`.
* **Begrenztes Nörgeln.** Solange das Doppel steht, meldet der Hook es erneut —
  aber höchstens `MAX_WARNINGS` mal je Session. Sonst könnte eine Session, die den
  Schaden gar nicht verursacht hat und ihn nicht beheben soll, in eine Endlosschleife
  aus Warnungen laufen.

Zum Registrieren in `.claude/settings.json` dieses Workspaces als PostToolUse-Hook
eintragen (siehe docs/USING-SUPERBOARD.md). Handtest:

    echo '{"session_id":"test"}' | python3 -m superboard.guard_hook; echo $?
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_WARNINGS = 3


def _stamp_path(session: str) -> Path:
    """Pro Session ein Stempel — sonst „stiehlt" eine parallele Session die Warnung.

    Zwei Board-Sessions laufen regelmäßig gleichzeitig. Mit EINEM globalen Stempel
    würde diejenige, deren Hook zuerst feuert, die mtime quittieren, und die Session,
    die den Splice wirklich gebaut hat, bekäme nichts zu sehen.
    """
    safe = "".join(c for c in session if c.isalnum() or c in "-_")[:64] or "nosession"
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    return tmp / f"gc-board-guard-{safe}.stamp"


def _read_stamp(path: Path) -> tuple[str, int]:
    try:
        parts = path.read_text().split("\n")
        return parts[0].strip(), int(parts[1]) if len(parts) > 1 else 0
    except (OSError, ValueError, IndexError):
        return "", 0


def _write_stamp(path: Path, mtime: str, warned: int) -> None:
    try:
        path.write_text(f"{mtime}\n{warned}")
    except OSError:
        pass  # fail-open: ohne Stempel prüft der nächste Aufruf halt erneut


def _summary(result: dict, limit: int = 4) -> list[str]:
    """Kurzfassung statt `board_lint._dup_report()`.

    Der lange Report ist für einen Menschen am Terminal richtig. Hier landet er im
    KONTEXT des Modells, und zwar potenziell nach jedem Tool-Call: gegen einen
    Schaden wie den beschriebenen wären das ~90 Zeilen je Warnung gewesen. Deshalb
    Kopfzahl plus die ersten Treffer — der Rest steht im Lint.
    """
    themes = result.get("dup_themes", [])
    ids = result.get("dup_ids", [])
    bodies = result.get("dup_bodies", [])
    out = [f"⚠ STRUKTUR — {len(themes)} doppelte Überschrift(en), "
           f"{len(ids)} doppelte @gc-id(s), {len(bodies)} inhaltsgleiche Item-Gruppe(n)", ""]
    for d in themes[:limit]:
        out.append(f"  Überschrift \"## {d['name']}\" steht {d['count']}× "
                   f"(Zeile {', '.join(str(x) for x in d['lines'])})")
    for d in ids[:limit]:
        loc = ", ".join(str(x) for x in d["lines"]) or "?"
        out.append(f"  @gc-id {d['id']} an {d['count']} Items (Zeile {loc}) — "
                   f"{(d['titles'] or [''])[0][:50]}")
    for d in bodies[:limit]:
        out.append(f"  Item \"{d['title'][:50]}\" {d['count']}× unter verschiedenen IDs "
                   f"({', '.join(d['ids'][:3])}{', …' if len(d['ids']) > 3 else ''})")
    rest = (max(0, len(themes) - limit) + max(0, len(ids) - limit)
            + max(0, len(bodies) - limit))
    if rest:
        out.append(f"  … und {rest} weitere.")
    return out + [""]


def _message(dup_block: list[str]) -> str:
    return "\n".join(
        ["⚠ board.md hat jetzt Struktur-Doppel — sehr wahrscheinlich durch den",
         "Schreibvorgang, der gerade gelaufen ist.", ""]
        + dup_block
        + ["Das ist der Splice-Unfall, den board_lint.py beschreibt: bei",
           "`s[:start] + neu + s[end:]` lag der End-Anker VOR dem Start-Anker, also",
           "steht die ganze Region dazwischen jetzt zweimal. Im größten bekannten Fall",
           "waren das 67 Items auf einen Schlag.",
           "",
           "Jetzt sofort reparieren, nicht weiterarbeiten: die überzählige Kopie",
           "entfernen — Schreibpfade treffen immer die ERSTE Kopie, dort steht also",
           "der neuere Arbeitsstand. Danach gegenprüfen mit:",
           "    python3 -m superboard.board_lint"])


def check() -> int:
    if os.environ.get("GC_BOARD_GUARD", "").strip().lower() in {"0", "off", "false", "no"}:
        return 0

    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        payload = {}

    sys.path.insert(0, str(HERE))
    from paths import BOARD

    if not BOARD.exists():
        return 0

    stamp = _stamp_path(str(payload.get("session_id", "")))
    mtime = str(BOARD.stat().st_mtime_ns)
    last, warned = _read_stamp(stamp)
    if last == mtime:
        return 0

    from board_lint import lint

    result = lint(BOARD.read_text())
    if not (result["dup_ids"] or result["dup_themes"] or result.get("dup_bodies")):
        _write_stamp(stamp, mtime, 0)
        return 0

    if warned >= MAX_WARNINGS:
        # Diese Session hat es dreimal gesagt und wird nicht gehört — quittieren und
        # ruhig sein. Der Befund bleibt in board_lint.py sichtbar.
        _write_stamp(stamp, mtime, warned)
        return 0

    # mtime bewusst NICHT quittieren: solange das Doppel steht, soll der nächste
    # Schreibvorgang wieder anschlagen.
    _write_stamp(stamp, last, warned + 1)
    print(_message(_summary(result)), file=sys.stderr)
    return 2


def main() -> int:
    try:
        return check()
    except Exception as exc:  # fail-open, aber nicht lautlos beim Handtest
        if os.environ.get("GC_BOARD_GUARD_DEBUG"):
            print(f"guard_hook: {exc!r}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
