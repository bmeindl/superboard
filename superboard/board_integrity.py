"""Integritäts-Prüfungen für board.md — die eine Quelle für „ist etwas verloren gegangen?".

WARUM getrennt von board_lint.py: `board_lint` prüft den ROUND-TRIP (überlebt jede Zeile
ein Parsen + Serialisieren?). Genau diese Prüfung ist blind für die teuerste Fehlerklasse,
die das Board kennt — eine still fehlende Pflichtangabe. Ein Item ohne `@gc-id` und ein
abgehaktes Item ohne Datum round-trippen beide tadellos; verloren ist trotzdem etwas.

Anlass für die Ausgründung (2026-08-25): Diese Prüfungen wohnten ursprünglich in einem
nächtlichen Repo-Hygiene-Skript und liefen damit NUR einmal pro Nacht. Ein Item verschwand
einmal für Stunden aus board.md (parallel überschrieben) — gefunden wurde es nur, weil
jemand den nächtlichen Guard zufällig von Hand laufen ließ. Entscheidung danach: Das Board
soll es selbst zeigen. Dafür brauchen beide Konsumenten dieselbe Logik, ohne dass
`server.py` ein ganzes Repo-Hygiene-Skript importieren muss.

Zwei Schweregrade, bewusst getrennt:

* `loss_issues()`  — Datenverlust. Etwas zeigt ins Leere: eine Faden-Datei ohne Item,
  ein `@gc-parent` ohne Eltern, ein Sidecar-Verweis ohne Datei. Das gehört in den
  Board-Kopf, weil es SOFORT gesehen werden muss.
* `hygiene_issues()` — reparierbare Schlampigkeit, kein Verlust: abgehaktes Item ohne
  Datum. Bleibt nächtlich. Wäre es im Kopf, könnte ein frisch angehaktes Item den
  Kopf für Minuten rot färben, ohne dass irgendetwas kaputt ist — und ein Indikator,
  der bei gesundem System redet, wird zu Rauschen.

Warum der Fund-Text so ausführlich ist: ein früherer Lauf meldete neun verwaiste
Faden-Dateien — alle neun echt, vernichtet durch `git stash drop` eines Sub-Agenten. Der
auswertende Lauf folgte dem damaligen Hinweis `git log -S <id> -- inbox/board.md`, bekam
null Treffer (die Items waren nie committet) und schloss daraus „gab es nie" — neun True
Positives als Fehlalarm abgetan. Ein Wächter, dessen mitgelieferter Prüfschritt bei der
teuersten Verlustklasse systematisch entlastet, ist gefährlicher als keiner. Der Hinweis
nennt deshalb jetzt zuerst den Faden-Inhalt als Unterscheider und ordnet leere
Git-Historie richtig ein.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import paths as _p

# Faden-Turn, dessen Volltext ausgelagert ist. Zeigt der Pfad ins Leere, ist der Inhalt weg.
SIDECAR_REF_RE = re.compile(
    r"→ (?:volle Antwort|voller Text): (inbox/gc-threads/[A-Za-z0-9._-]+\.md)\s*$", re.MULTILINE)
# Über die Zeit sammeln sich Faden-Dateien von Items an, die BEWUSST gelöscht wurden.
# Die dauerhaft zu melden macht den Guard chronisch rot. Ein ID-Verlust fällt am selben
# Tag an — ein Fenster reicht, danach schweigt er wieder.
ORPHAN_WINDOW_DAYS = 7


ID_LINE_RE = re.compile(r"^\s*@gc-id:\s*([0-9a-f]{12})\s*$", re.MULTILINE)


def _known_ids(text: str, archive: Path) -> set[str]:
    """Alle IDs, die es legitim gibt — Board UND Archiv. Abgehakte Items wandern per
    sweep.py nach board-archive.md, ihre IDs verschwinden also zu Recht aus board.md.

    Beide Hälften lesen dieselbe `@gc-id:`-ZEILE. Eine frühere Fassung sammelte in der
    Archiv-Hälfte stattdessen jede 12-stellige Hex-Folge im gesamten Archivtext ein — also
    auch IDs, die dort nur in Fließtext ZITIERT werden. Ein aktives Item, dessen ID irgendwo
    im Archiv erwähnt ist, galt damit dauerhaft als „bekannt" und war von der
    Verwaisungs-Prüfung ausgenommen — gemessen traf das rund 10 % der aktiven Items. Sein
    Verschwinden aus board.md hätte niemand gemeldet. Die enge Fassung meldete keinen
    einzigen zusätzlichen Fund; sie ist strikt präziser, nicht lauter."""
    ids = set(ID_LINE_RE.findall(text))
    if archive.exists():
        ids |= set(ID_LINE_RE.findall(archive.read_text()))
    return ids


def loss_issues(board: Path | None = None, archive: Path | None = None,
                threads: Path | None = None, root: Path | None = None) -> list[str]:
    """Alles, wo eine Adresse ins Leere zeigt. Fail gracefully: nichts crasht, aber der
    Befund wird sichtbar — im Board-Kopf sofort, im Morgen-Digest nochmal."""
    board = board or _p.BOARD
    archive = archive or _p.ARCHIVE
    threads = threads or _p.THREADS
    root = root or _p.GC_ROOT
    if not board.exists():
        return []
    text = board.read_text()
    issues: list[str] = []

    for ref in SIDECAR_REF_RE.findall(text):
        if not (root / ref).is_file():
            issues.append(f"board.md: toter Sidecar-Verweis → {ref}")

    ids = _known_ids(text, archive)
    for parent in sorted(set(re.findall(r"^\s*@gc-parent:\s*([0-9a-f]{12})\s*$", text, re.MULTILINE))):
        if parent not in ids:
            issues.append(f"board.md: @gc-parent {parent} zeigt auf kein existierendes Item — "
                          "Eltern-Item gelöscht oder hat seine @gc-id verloren")

    if not threads.is_dir():
        return issues
    cutoff = datetime.now().timestamp() - ORPHAN_WINDOW_DAYS * 86400
    seen: set[str] = set()
    for f in sorted(threads.glob("*.md")):
        gc_id = f.name[:12]
        if gc_id in ids or gc_id in seen or not re.fullmatch(r"[0-9a-f]{12}", gc_id):
            continue
        # Test-Fixtures (aaaa…, bbbb…) landen gelegentlich im echten Ordner — kein Board-Defekt.
        if len(set(gc_id)) < 4 or f.stat().st_mtime < cutoff:
            continue
        seen.add(gc_id)
        issues.append(f"board.md: Faden-Datei {f.name} gehört zu keinem Item mehr — "
                      f"Item {gc_id} gelöscht oder hat seine @gc-id verloren. "
                      f"ZUERST den Faden lesen (head -3 inbox/gc-threads/{gc_id}-*.md): trägt er "
                      f"einen echten Item-Titel, ist es ein Verlust. Dann Wiederherstellung suchen: "
                      f"git log -S {gc_id} -- inbox/board.md, und falls das LEER bleibt, "
                      f"git fsck --unreachable / git stash list / git reflog — "
                      f"leere Git-Historie heißt NICHT \"gab es nie\", sondern meist "
                      f"\"nie committet und deshalb restlos weg\"")
    return issues


def hygiene_issues(board: Path | None = None) -> list[str]:
    """Abgehaktes Item OHNE Datum — der Sweep erreicht es nie wieder.

    Anlass: Ein Agent legte ein Item an und hakte es am selben Tag ab, ohne je ein Datum
    zu setzen. `sweep.done_at()` liest `@done-at`, sonst das `date`-Feld — fehlen beide,
    gibt es keinen Reifezeitpunkt, und das Item bleibt dauerhaft als erledigte Karteileiche
    im aktiven Board stehen.

    Geprüft wird die Titelzeile: ein Item trägt sein Datum als `*(YYYY-MM-DD)*` dahinter.
    Fehlt es UND fehlt `@done-at:` im Block darunter, ist das Item unerreichbar."""
    board = board or _p.BOARD
    if not board.exists():
        return []
    issues: list[str] = []
    lines = board.read_text().split("\n")
    for i, line in enumerate(lines):
        if not re.match(r"^- \[[xX]\] ", line):
            continue
        if re.search(r"\*\(\d{4}-\d{2}-\d{2}\)\*\s*$", line):
            continue
        block: list[str] = []
        for nxt in lines[i + 1:]:
            if re.match(r"^(- \[|#{1,3} )", nxt):
                break
            block.append(nxt)
        if any(b.strip().startswith("@done-at:") for b in block):
            continue
        gc_id = next((b.strip()[8:].strip() for b in block if b.strip().startswith("@gc-id:")), "?")
        title = re.sub(r"^- \[[xX]\] ", "", line).strip("* ")[:60]
        issues.append(f"board.md: abgehaktes Item ohne Datum → {gc_id} „{title}“ — "
                      "sweep.py findet keinen Reifezeitpunkt und archiviert es nie "
                      "(Fix: @done-at: oder *(YYYY-MM-DD)* setzen)")
    return issues


def all_issues(**kw) -> list[str]:
    """Beide Klassen — für den nächtlichen Health-Check, der nicht unterscheidet."""
    return loss_issues(**kw) + hygiene_issues(board=kw.get("board"))


if __name__ == "__main__":
    for line in all_issues():
        print(line)
