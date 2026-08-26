#!/usr/bin/env python3
"""Sidecar-Auslagerung langer Faden-Turns nach inbox/gc-threads/.

board.md-Diät (beschlossen 2026-07-16): Faden-Turns sind Markdown-EINZEILER.
Lange/mehrzeilige Turns (Paste-backs des Owners genauso wie Agent-Antworten) wandern
komplett in eine Sidecar-Datei; inline bleibt der erste Satz + typisierter Verweis.
Nichts wird gekürzt — nur verschoben (Invariante 1: der Verweis IST der board.md-
Inhalt, der Volltext liegt daneben, Roundtrip bleibt verlustfrei).

Gemeinsames Modul für server.py (Append-Endpoint: @gc:-Turns vom Owner),
gc_runner.py (Antworten + Prompt-Expansion) und migrate_diet.py (Altbestand).
Vorher lebte die Logik nur in gc_runner._inline_reply und deckte asymmetrisch
NUR Antworten ab — die @gc:-Turns des Owners machten 52 % der board.md-Bytes aus.

Härtung (ext. Review GPT-5.6, 2026-07-16): Verweis-Erkennung ist streng typisiert
(exaktes Zeilenende-Muster, basename-only, resolve() muss unter gc-threads/ bleiben)
— board.md-Text ist Benutzereingabe, ein zitierter Verweis mitten im Satz darf NIE
zur Datei-Expansion führen.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

import config as _cfg
import paths as _p
from markers import REF_LABEL, REF_RE  # noqa: F401  (REF_RE: Datenformat, siehe markers.py)

GC_ROOT = _p.GC_ROOT
SIDECAR_DIR = _p.THREADS
INLINE_MAX = 500          # längere/mehrzeilige Turns wandern in einen Sidecar
SUMMARY_MAX = 200         # Inline-Kurzsatz (satzweise gekappt, kein Hard-Cut)
EXPAND_MAX = 30_000       # Prompt-Expansion: Obergrenze pro Turn, mit sichtbarem Marker

# Kopfzeile der Sidecar-Datei. Reiner Lesetext fuer den Menschen, KEIN Parse-Ziel
# (expand() liest die Datei als Ganzes) - darf uebersetzt werden, anders als markers.py.
HEADER_LABEL = {
    "ask": f"{_cfg.OWNER} turn",
    "reply": "Board agent reply",
    "done": "Thread closed",
}

# Zeitanteil eines Sidecar-Dateinamens, streng am Ende verankert: `-YYYYMMDD-HHMMSS-xxxx.md`.
# Nur der Suffix ist Format — der Rest des Namens ist die gc-id und darf alles sein.
_TS_RE = re.compile(r"-(\d{8})-(\d{6})-[0-9a-f]{4}\.md$")


def write_sidecar(gc_id: str, title: str, full_text: str,
                  sidecar_dir: Path | None = None, kind: str = "reply") -> Path:
    """Volltext eines Turns als eigene Datei ablegen. Append-only: Sidecars werden
    nie umbenannt oder überschrieben (Merge-Sicherheit Mac↔cloud)."""
    sidecar_dir = sidecar_dir or SIDECAR_DIR
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    path = sidecar_dir / f"{gc_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.md"
    path.write_text(f"# {HEADER_LABEL.get(kind, 'Thread turn')}: {title}\n\n"
                    f"*{datetime.now().strftime('%Y-%m-%d %H:%M')} · Item @gc-id: {gc_id}*\n\n"
                    f"{full_text}\n")
    return path


def turn_time(text: str) -> str | None:
    """Entstehungszeit eines Faden-Turns — aus dem Dateinamen seines Sidecars.

    Ausgelagerte Turns tragen ihre Zeit schon im Namen (siehe `write_sidecar`); wir
    lesen sie nur zurück, statt ein zweites Feld in `board.md` zu erfinden. Deshalb
    steht die Zeit hier und nicht bei den Lesern: wer den Namen BAUT, besitzt auch
    das Format. Turns, die kurz genug für die Zeile selbst waren, haben keine Datei
    und bekommen keine Zeit — geschätzt wird nicht.
    """
    m = REF_RE.search(text or "")
    if not m or not (ts := _TS_RE.search(m.group(1))):
        return None
    d, hms = ts.group(1), ts.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:]} {hms[:2]}:{hms[2:4]}"


def _summary(text: str) -> str:
    """Erste Zeile, satzweise auf SUMMARY_MAX gekappt — Fragmente wie „…steh…"
    lasen sich als kaputte Antwort (2026-07-14)."""
    first = text.split("\n", 1)[0].strip()
    if len(first) <= SUMMARY_MAX:
        return first
    head = first[:SUMMARY_MAX]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    return head[:cut + 1] if cut >= 40 else head + "…"


def inline_turn(gc_id: str, title: str, text: str,
                sidecar_dir: Path | None = None, kind: str = "reply") -> str:
    """Faden-Turn → board.md-taugliche Zeile. Kurz: unverändert (kurze mehrzeilige
    @gc:-Notizen plättet der Server-Belt wie bisher zu `·`-Einzeilern). Lang — oder
    bei Antworten auch mehrzeilig (agent-formatiertes Markdown, Plätten zerstört
    Listen/Code): Volltext → Sidecar, inline Kurzsatz + typisierter Verweis."""
    flat = text.strip()
    needs_sidecar = len(flat) > INLINE_MAX or (kind == "reply" and "\n" in flat)
    if not needs_sidecar:
        return flat
    if REF_RE.search(flat.split("\n", 1)[0]):
        return flat.split("\n", 1)[0].strip()  # trägt schon einen Verweis — nie doppelt auslagern
    path = write_sidecar(gc_id, title, flat, sidecar_dir, kind)
    try:
        ref = path.relative_to(GC_ROOT)
    except ValueError:  # Sidecar-Dir außerhalb des Repos (z.B. Tests)
        ref = path
    return f"{_summary(flat)} … → {REF_LABEL.get(kind, 'full text')}: {ref}"


def expand(text: str, sidecar_dir: Path | None = None) -> str | None:
    """Trägt der Turn am ZEILENENDE einen Sidecar-Verweis: Volltext der Datei —
    sonst None. Gehärtet: basename-only aus REF_RE, Pfad muss nach resolve() unter
    gc-threads/ liegen (kein Traversal, kein Symlink-Ausbruch). Fail gracefully:
    fehlende/unlesbare Datei → None, der Caller behält die Kurzzeile."""
    m = REF_RE.search(text)
    if not m:
        return None
    sidecar_dir = (sidecar_dir or SIDECAR_DIR).resolve()
    p = (sidecar_dir / m.group(1)).resolve()
    if not p.is_relative_to(sidecar_dir) or not p.is_file():
        return None
    try:
        full = p.read_text()
    except OSError:
        return None
    if len(full) > EXPAND_MAX:
        full = full[:EXPAND_MAX] + f"\n[… truncated to {EXPAND_MAX} characters — remainder in {m.group(1)}]"
    return full
