#!/usr/bin/env python3
"""Bump Superboard's internal build stand mechanically before a code commit.

The script chooses the level from the conventional commit type and changed-code
size, then keeps the internal build in ``server.py``, ``CHANGELOG.md`` and the UI
reload stamp in sync. The public package version in ``pyproject.toml`` is a separate
release track and is changed only by the release ritual:

    fix:  → immer patch (auch ein großer Fix bleibt ein Fix)
    feat: → minor ab MINOR_LINES · sonst patch. major NEVER automatically — from
            MAJOR_HINT_LINES on the script only SUGGESTS a new main number.

Only code files below ``superboard/`` are counted; documentation-only commits do
not bump the application version. Neither does instance CONTENT — ``actions.json``,
``rituals.json`` and ``board.config.json`` (CONTENT_FILES) hold what this one
installation is set up to do, not what the board can do. The number describes
release scope, not API compatibility.

Churn is ``max(added, deleted)`` per file, so a replaced line counts once. New
files count in full.

Benutzung, vor dem Commit:

    python3 superboard/bump.py "feat(ui): add a focus view"
    python3 superboard/bump.py "fix(runner): keep receipts" --dry-run
    python3 superboard/bump.py "feat: add a workflow" --minor

Use ``--major``, ``--minor`` or ``--patch`` when the automated level is not the
release level you intend.


This number is the internal build stand (``server.py``'s ``VERSION``), not the
public release number. The public number lives in ``pyproject.toml`` and moves
only at a deliberate release, via ``port_to_superboard.py release`` in the repo
Superboard is projected from — never here, never on every commit. Two numbers,
two audiences: the build stand measures scope, the public number is a promise.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
CHANGELOG = HERE / "CHANGELOG.md"
INDEX = HERE / "index.html"

CODE_SUFFIXES = {".py", ".html", ".sh", ".json", ".plist", ".css", ".js"}
# Instance content, not kernel: a new cockpit action or ritual is a SETTING of this
# installation. It says nothing about what the board can do, so it must not move the
# number. `.json` is in CODE_SUFFIXES for real code, hence the explicit exception.
CONTENT_FILES = {"actions.json", "rituals.json", "rituale.json", "board.config.json"}
MINOR_LINES = 200
MAJOR_HINT_LINES = 1000   # from here on only a SUGGESTION, never an automatic major

VERSION_RE = re.compile(r'^VERSION = "([^"]+)"', re.M)
HEADING_RE = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.M)
APP_VERSION_RE = re.compile(r'const APP_VERSION = "([^"]+)"')


def read_version() -> tuple[int, int, int]:
    m = VERSION_RE.search(SERVER.read_text(encoding="utf-8"))
    if not m:
        sys.exit("VERSION-Zeile in server.py nicht gefunden")
    major, minor, patch = m.group(1).split(".")
    return int(major), int(minor), int(patch)


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                              text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.exit(f"git {args[0]} fehlgeschlagen: {exc}")


def numstat_churn(numstat: str) -> tuple[int, list[str]]:
    """Berührte Code-Zeilen aus `git diff --numstat`: `max(added, deleted)` je Datei.

    Eine ERSETZTE Zeile ist eine berührte Zeile, keine zwei (13.08., Faden
    `6aa4dbc3a873`, Frage 2 → A). Vorher zählte hier `added + deleted`, was jeden
    Rewrite doppelt bewertete: die Englisch-Umstellung ersetzte in `index.html`
    632 Zeilen 1:1 und landete damit bei 1.265 statt 632. Gerechnet an den echten
    Commits verschiebt die Umstellung genau die Umbenennungs-Sweeps (5.0.0: 2.526 →
    1.361) und lässt normale Features fast unberührt (4.0.0: 601 → 554).
    """
    total, files = 0, []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if Path(path).suffix not in CODE_SUFFIXES or added == "-":   # "-" = binär
            continue
        if Path(path).name in CONTENT_FILES:                         # Inhalt, kein Kernel
            continue
        total += max(int(added), int(deleted))
        files.append(Path(path).name)
    return total, files


def code_churn() -> tuple[int, list[str]]:
    """Berührte Code-Zeilen unter superboard/ (Zählweise: numstat_churn).

    Ungetrackte Dateien zählen VOLL mit: `git diff HEAD` sieht sie nicht, und ein
    Commit, der ein ganzes neues Modul mitbringt (wie seinerzeit receipt.py), wäre
    sonst als 0-Churn durchgerutscht — also ausgerechnet der klarste minor-Fall.
    """
    total, files = numstat_churn(_git("diff", "HEAD", "--numstat", "--", str(HERE)))

    root = Path(_git("rev-parse", "--show-toplevel").strip())
    for rel in _git("ls-files", "--others", "--exclude-standard", "--full-name",
                    "--", str(HERE)).splitlines():
        path = root / rel
        if path.suffix not in CODE_SUFFIXES or path.name in CONTENT_FILES:
            continue
        try:
            total += len(path.read_text(encoding="utf-8").splitlines())
            files.append(path.name)
        except (OSError, UnicodeDecodeError):
            continue

    return total, files


def decide(note: str, churn: int, forced: str | None) -> tuple[str, str]:
    """(level, begruendung)"""
    if forced:
        return forced, "übersteuert per Flag"
    typ = note.split("(")[0].split(":")[0].strip().lower()
    if typ.startswith("fix"):
        return "patch", "fix → immer patch"
    if churn >= MINOR_LINES:
        return "minor", f"{churn} Code-Zeilen ≥ {MINOR_LINES}"
    return "patch", f"{churn} Code-Zeilen < {MINOR_LINES}"


def bump(cur: tuple[int, int, int], level: str) -> str:
    major, minor, patch = cur
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_version(new: str) -> None:
    text = SERVER.read_text(encoding="utf-8")
    text, n = VERSION_RE.subn(f'VERSION = "{new}"', text, count=1)
    if n != 1:
        sys.exit("VERSION-Zeile konnte nicht ersetzt werden")
    SERVER.write_text(text, encoding="utf-8")


def next_app_version(cur: str, today: str) -> str:
    """Nächster Auto-Reload-Stempel für `index.html`: Datum + Buchstabenzähler.

    `2026-07-28c` → `2026-07-28d`, an einem neuen Tag → `<heute>a`, und `…z` → `…aa`
    (an manchen Tagen gibt es mehr als 26 Frontend-Commits, das ist kein Hilfsargument
    sondern gemessene Realität dieses Repos)."""
    if not cur.startswith(today):
        return today + "a"
    suffix = list(cur[len(today):]) or ["`"]  # "`" ist 'a'-1: leerer Suffix → "a"
    for i in range(len(suffix) - 1, -1, -1):
        if suffix[i] != "z":
            suffix[i] = chr(ord(suffix[i]) + 1)
            return today + "".join(suffix)
        suffix[i] = "a"
    return today + "a" + "".join(suffix)


def write_app_version() -> tuple[str, str] | None:
    """`APP_VERSION` in index.html mitziehen. Rückgabe (alt, neu) oder None.

    Warum das hier hängt und nicht in einem Test (28.07.): `APP_VERSION` ist der Stempel,
    an dem ein offener Board-Tab merkt, dass er neues JS laden muss. Vergisst man ihn,
    läuft ein offener Tab unbemerkt auf altem Frontend weiter — dieselbe Klasse von
    Zustands-Drift wie ein Server, der alten Code im Speicher hält, nur clientseitig.
    Ein Test hätte das erst NACH dem Commit angemahnt; hier kann es gar nicht erst
    passieren. Bewusst getrennt von `VERSION`: ein reiner Server-Change soll keine
    grundlosen Tab-Reloads auslösen, deshalb bumpt das nur bei index.html-Churn."""
    text = INDEX.read_text(encoding="utf-8")
    m = APP_VERSION_RE.search(text)
    if not m:
        sys.exit("APP_VERSION-Zeile in index.html nicht gefunden")
    new = next_app_version(m.group(1), date.today().isoformat())
    INDEX.write_text(APP_VERSION_RE.sub(f'const APP_VERSION = "{new}"', text, count=1),
                     encoding="utf-8")
    return m.group(1), new


def write_changelog(new: str, note: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    m = HEADING_RE.search(text)
    if not m:
        sys.exit("Keine Versions-Überschrift in CHANGELOG.md gefunden")
    block = f"## [{new}] — {date.today().isoformat()}\n- {note}\n\n"
    CHANGELOG.write_text(text[: m.start()] + block + text[m.start():], encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help='Changelog-Zeile, konventionell: "feat(ui): …" / "fix(gc): …"')
    ap.add_argument("--major", action="store_const", const="major", dest="forced")
    ap.add_argument("--minor", action="store_const", const="minor", dest="forced")
    ap.add_argument("--patch", action="store_const", const="patch", dest="forced")
    ap.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts schreiben")
    args = ap.parse_args()

    churn, files = code_churn()
    if churn == 0 and not args.forced:
        print("Keine Code-Änderung unter superboard/ — kein Bump"
              " (Doku und Instanz-Inhalt bumpen nicht).")
        return

    level, why = decide(args.note, churn, args.forced)
    cur = read_version()
    new = bump(cur, level)
    old = ".".join(map(str, cur))

    print(f"{old} → {new}  ({level}, {why})")
    if churn >= MAJOR_HINT_LINES and level != "major":
        print(f"  Note: {churn} touched code lines — that is a chunk. Should this be a new"
              f" main number? Then `--major` (the call belongs to the owner).")
    if files:
        print(f"  Code-Dateien: {', '.join(sorted(set(files)))}")
    if args.dry_run:
        print("  --dry-run: nichts geschrieben")
        return

    write_version(new)
    write_changelog(new, args.note)
    geschrieben = ["server.py", "CHANGELOG.md"]
    # Frontend geändert → Auto-Reload-Stempel mitziehen, sonst hängt ein offener Tab
    # unbemerkt auf altem JS (siehe write_app_version).
    if "index.html" in files:
        alt, neu = write_app_version()
        geschrieben.append("index.html")
        print(f"  APP_VERSION: {alt} → {neu}  (offene Tabs laden neu)")
    print(f"  {' + '.join(geschrieben)} geschrieben — jetzt committen.")


if __name__ == "__main__":
    main()
