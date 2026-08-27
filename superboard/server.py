#!/usr/bin/env python3
"""todo-board — lokaler Mini-Server für inbox/board.md.

Rendert das Board als Web-App (Matrix Themen × Jetzt/Bald/Geparkt + Personen-Listen)
und schreibt jede Interaktion SOFORT zurück ins Markdown (Autosave, kein Save-Button).
Quelle der Wahrheit ist die Markdown-Datei — Claude und der Owner editieren dieselbe.

Start:  python3 server.py            (Port 47822, board: ../../inbox/board.md)
        python3 server.py --port N --file /pfad/board.md
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import board_lint  # Round-Trip-Diagnose: WELCHE Zeilen ein Save vernichten würde (28.07.)
import sidecar  # geteilte Auslagerungs-Logik langer Faden-Turns — board.md-Diät 2026-07-16

import config as _cfg  # Instanz-Config (Name, Identitaeten) - siehe config.py
import paths as _p  # die EINE Pfad-Herleitung (vorher 9 unabhaengige Kopien)
import receipt_hook as _receipt  # optionale Telemetrie; ohne Modul bleibt der Kern lauffähig
import registries as _registries  # validierte lokale Actions/Rituale mit Diagnosen
from claude_identity import default_claude_env

ROOT = _p.HERE
GC_ROOT = _p.GC_ROOT                   # Repo-Wurzel der origin instance (cwd fuer Subprozesse)
DEFAULT_BOARD = _p.BOARD
# Internal board build, used to trace which code is running. Public package releases
# use the separate version in pyproject.toml; an internal bump must never overwrite it.
# (APP_VERSION in index.html is only the browser auto-reload stamp.)
VERSION = "6.22.0"
# A workspace may carry an identity wrapper at tools/claude-identities/claude-private —
# scripts/testrig.sh writes exactly that file so a rig run cannot inherit the operator's
# Claude settings, skills and MCP servers. A normal installation has no such file and gets
# the plain `claude` on PATH. Deliberately NO env override: the parent environment must not
# be able to redirect which binary a run uses (test_plain_binary_cannot_be_switched_by_parent_env).
_CLAUDE_WRAPPER = GC_ROOT / "tools" / "claude-identities" / "claude-private"
DEFAULT_CLAUDE_BIN = str(_CLAUDE_WRAPPER) if _CLAUDE_WRAPPER.is_file() else "claude"
CLAUDE_BIN = DEFAULT_CLAUDE_BIN


def claude_binary() -> str:
    """Explicit default Claude executable; tests replace the constant directly."""
    return CLAUDE_BIN


def current_version() -> str:
    """VERSION frisch von der Platte lesen statt aus dem Modul-Konstanten-Cache.

    Sonst zeigt der Header bis zum nächsten Server-Neustart eine veraltete Nummer —
    und genau als Frische-Signal will der Owner sie benutzen ("ein Zeichen, das ich noch
    nicht aktualisiert habe", 21.07.). A board-agent run cannot replace its own parent
    process, so the number must update without a restart. Errors fall back to VERSION.
    """
    try:
        m = re.search(r'^VERSION = "([^"]+)"', Path(__file__).read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else VERSION
    except Exception:
        return VERSION
# Spalten sind pro Thema wählbar (Owner-Entscheidung, 2026-07-14): das Dev-Board hat eine vierte,
# "Wartet auf andere" — für Items, bei denen der Ball NICHT bei ihm liegt (MR wartet auf Review,
# Kollege muss ein Flag setzen). Die Achse bleibt Dringlichkeit; "Wartet" ist eine echte
# Lebenszyklus-Stufe, keine Quellen-Kategorie (deshalb ok, anders als die verworfene "Tracker"-Spalte).
# "Termine" (Owner-Entscheidung 2026-07-28, Faden 0662da9c2603 — „ich sehe mein Follow-up nicht im Board,
# nur unten in der Leiste"): analog zu "Wartet auf andere" eine fünfte, opt-in Spalte für
# @on:-Termin-To-dos — rein ziehen setzt/fragt das Datum, raus ziehen löscht es (index.html
# dropItem). Ersetzt NICHT das alte @on-Verhalten (Matrix-Hiding bis zum Stichtag, Heute-
# Leiste-Pill) — das bleibt für Items, die NICHT in dieser Spalte liegen.
# KNOWN_COLUMNS = kanonische Reihenfolge beim Serialisieren. Ein Thema hat die Spalten, die in
# der md stehen — kein Thema bekommt Spalten aufgezwungen, die es nicht hat.
KNOWN_COLUMNS = ["Jetzt", "Wartet auf andere", "Termine", "Bald", "Geparkt"]
DEFAULT_COLUMNS = ["Jetzt", "Bald", "Geparkt"]
COLUMNS = DEFAULT_COLUMNS  # Rückwärtskompatibler Alias (sweep.py)

# File-boundary translation: board.md is a file the user reads and hand-edits directly,
# so its headings should read in English — but the INTERNAL dict keys above ("Jetzt" etc.)
# stay as they are. They are identifiers, not user-visible text, used in ~300 places
# (theme["cols"]["Jetzt"], KNOWN_COLS in index.html, WAIT_COL in sweep.py, …); renaming
# them is pure churn for zero benefit and a large regression surface. Only the on-disk
# spelling changes, and only at the parse/serialize boundary in THIS file:
#   PARSE     accepts EITHER the English name below OR the legacy internal (German) name
#             — a hand-edited file, an old file, or one written by an older running
#             server must keep working forever; this is not a one-shot migration.
#   SERIALISE always emits the English name.
# This dict is the single source of truth for that boundary — column_key()/section_key()
# below are the only places allowed to normalise a heading string; nowhere else may
# hardcode a heading literal on either side. English names match the UI's COL_LABELS
# (index.html) so the on-disk spelling matches what the user sees rendered.
COLUMN_FILE_NAMES = {
    "Jetzt": "Now",
    "Wartet auf andere": "Waiting on others",
    "Termine": "Dates",
    "Bald": "Next",
    "Geparkt": "Backlog",
}
SECTION_FILE_NAMES = {
    "Personen": "To discuss",
    "Notizen": "Notes",
}
_COLUMN_FROM_FILE_NAME = {v: k for k, v in COLUMN_FILE_NAMES.items()}
_SECTION_FROM_FILE_NAME = {v: k for k, v in SECTION_FILE_NAMES.items()}


def column_key(name: str) -> str | None:
    """Normalise a `### `-heading token (on-disk English name OR legacy internal/German
    name) to the internal column key. None if it's neither — same as the old inline
    `name if name in KNOWN_COLUMNS else None` check, just also accepting English."""
    name = name.strip()
    if name in _COLUMN_FROM_FILE_NAME:
        return _COLUMN_FROM_FILE_NAME[name]
    return name if name in KNOWN_COLUMNS else None


def section_key(name: str) -> str | None:
    """Same normalisation for `# `-section headings (Personen/To discuss, Notizen/Notes)."""
    name = name.strip()
    if name in _SECTION_FROM_FILE_NAME:
        return _SECTION_FROM_FILE_NAME[name]
    return name if name in SECTION_FILE_NAMES else None


# Every spelling of the Notizen/Notes section heading the parser accepts — used wherever
# a raw-text scan needs to find the notes boundary without going through parse_board
# (guard_scope, _block_window: both work on raw text, before/without a parsed board).
_NOTES_HEADS = {"# Notizen", f"# {SECTION_FILE_NAMES['Notizen']}"}

# Der Personen-Tab ist faktisch ein Besprechungsthemen-Board — Personen sind nur der
# häufigste Anlass, Meetings (z.B. "Mittwoch Domain Alignment") der zweithäufigste
# (28.07., Board-Item „Personen-Board = Besprechungsthemen-Board"). Statt eines
# zweiten Arrays trägt ein Meeting-Eintrag nur `kind: "meeting"` — dieselbe Liste
# `board["persons"]", nur die Überschrift im md trägt den 📅-Marker und index.html
# rendert ihn in einer zweiten Swimlane. Hält jede id-/drag-/sweep-Logik unverändert,
# die über `board["persons"]` iteriert (Entscheidung 06.08., Faden e84e15b8c6ba:
# "Unterteilt das in zwei Swimlines").
MEETING_MARK = "📅 "


def theme_cols(theme: dict) -> list[str]:
    """Spalten dieses Themas in kanonischer Reihenfolge."""
    return [c for c in KNOWN_COLUMNS if c in theme["cols"]]


def _all_cols(board: dict):
    for th in board["themes"]:
        for c in theme_cols(th):
            yield th, c


# Clickable thread-file links are default-deny. Personal/private top-level
# folders are not served, even on localhost; dotfiles, unknown suffixes, files
# above 2 MB and paths outside the workspace are blocked below as well.
BLOCKED_TOP_DIRS: set[str] = {"personal", "private"}
READABLE_SUFFIXES = {".md", ".txt", ".py", ".ts", ".js", ".html", ".css",
                     ".json", ".yaml", ".yml", ".sh", ".csv", ".sql", ".toml"}
WRITE_LOCK = threading.Lock()


@contextmanager
def board_write_guard(board_path: Path):
    """Schreibschutz für board.md — Thread- UND Prozess-Ebene (P0-Fix 2026-07-16,
    ext. Review GPT-5.6): WRITE_LOCK deckt nur Threads IN diesem Prozess; sweep.py
    und migrate_diet.py schreiben als eigene Prozesse und konnten einen parallelen
    Append still verlieren. flock auf einer Lock-Datei neben board.md — JEDER
    Schreibpfad (Server-Endpoints, sweep, Migration) nimmt denselben Guard."""
    with WRITE_LOCK:
        lock_path = board_path.with_name(board_path.name + ".lock")
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
# Prozessstart dieser Instanz — der einzige Weg, "zuletzt neugestartet" zu wissen: die
# Registry (RUNNING etc.) lebt nur im Prozess, also ist ihr Fehlen selbst kein Signal.
# Board-Status-Zeile (28.07.) liest das über /api/cockpit.
SERVER_START = time.time()
# Board-Agent-Runs: gc-id → Startzeit. Der Server ist der einzige Spawner (UI-Flow),
# darum reicht In-Memory als Doppel-Run-Schutz. Server-Neustart mitten im Run:
# Registry weg, Pill verschwindet, Item bleibt for_gc → einfach nochmal RUN.
RUNNING: dict[str, float] = {}
# Lebenszeichen des laufenden Runs: gc-id → {steps, last_tool, session_id, rate_limit,
# last_event, stop_path}. BEWUSST neben RUNNING statt darin: RUNNING ist an sehr vielen
# Stellen als "gc-id → Startzeit" gelesen (running_since fürs Frontend), und ein
# Typwechsel dort wäre eine breite Umbaustelle für einen schmalen Gewinn.
# Gefüllt vom on_beat-Callback aus gc_runner.watch_run (2026-07-27).
BEATS: dict[str, dict] = {}
QUEUED: dict[str, float] = {}  # Run-all-Warteschlange (wartet auf freien Parallel-Slot)
# Items, deren RUNNING-Eintrag eine /compact-Kompaktierung ist (kein Agent-Arbeitslauf) —
# die UI beschriftet den Zustand dann als "Kontext wird kompaktiert" statt "Agent läuft".
COMPACTING: set[str] = set()
COMPACT_TIMEOUT = 600  # /compact fasst nur zusammen — 10 min sind großzügig
RUN_LOCK = threading.Lock()
BOX_RE = re.compile(r"(?m)^\s*- \[[ xX]\]")

ITEM_RE = re.compile(r"^- \[([ xX])\] (.*)$")
SUB_RE = re.compile(r"^\s+- \[([ xX])\] (.*)$")
# GC-Faden Owner↔Agent: EXAKTE Tags (kein optionales `:` mehr — sonst
# fräße @gc: auch @gc-re:/@gc-done: als Text). Reihenfolge = Faden.
#   @gc:      = Turn vom Owner (Frage/Auftrag)  -> kind "ask"
#   @gc-re:   = Turn vom Agenten (Antwort)   -> kind "reply"
#   @gc-done: = Meta-Event "Faden geschlossen"      -> kind "done"
#   @gc-sys:  = System-Turn (Roll-up eines Sub-Fadens)   -> kind "sys"
GC_DONE_RE = re.compile(r"^\s+@gc-done:\s?(.*)$")
GC_REPLY_RE = re.compile(r"^\s+@gc-re:\s?(.*)$")
GC_ASK_RE = re.compile(r"^\s+@gc:\s?(.*)$")
# @gc-sys: = System-Turn, den KEIN Mensch und kein Agent geschrieben hat (heute: der
# Sub-Roll-up). thread_status ignoriert ihn — sonst kippte ein automatischer Roll-up das
# Item auf „GC hat geantwortet, du bist dran" (Sol-Befund 1, 2026-07-23). Er ist trotzdem
# ein echter Faden-Event (Reihenfolge, Serialisierung, lost-Guard), kein Body-Text.
GC_SYS_RE = re.compile(r"^\s+@gc-sys:\s?(.*)$")
# Kind -> Tag, die EINE Quelle der Wahrheit fürs Zurückschreiben eines Faden-Turns.
# Vorher hielt sweep.py eine eigene Kopie dieser Map (`_GC_TAG`) — die kannte "sys" nicht
# und riss ab dem 27.07. jede Nacht den kompletten Sweep mit KeyError ab (nichts archiviert,
# board.md wuchs auf 250 KB). Ein neuer Kind darf nur noch HIER entstehen; wer serialisiert,
# importiert diese Map (sweep.gc_tag fällt zusätzlich generisch zurück statt zu crashen).
from markers import GC_TAG  # noqa: E402,F401  (Datenformat - Definition + Begruendung dort)
THREAD_LINE_RE = re.compile(r"(?m)^\s+@gc(?:-re|-done|-sys)?:")  # für lost-Guard
# Session-Pointer pro Item (der Board-Agent-Runner schreibt ihn beim ersten Run):
# Item-Attribut, KEIN Faden-Event. UUID kanonisch (+ optional " · name" als Label).
# @gc-session: matcht KEINE der Faden-Regexes oben (@gc: braucht ':' direkt nach @gc).
GC_SESSION_RE = re.compile(r"^\s+@gc-session:\s?(.*)$")
GC_SESSION_LINE_RE = re.compile(r"(?m)^\s+@gc-session:")  # für lost-Guard
# @gc-sessions: (Plural) = Verlaufsliste ABGELÖSTER Resume-Pointer (10.08.: "Liste
# alter Session-UUIDs am Item mitführen" — nach einem Kontext-Schnitt/Neustart will er
# zur vorigen Session zurückblättern können, die @gc-session: sonst kommentarlos
# überschreibt). Bricht NICHT die geprüfte Invariante "genau eine @gc-session:-Zeile" —
# eigener Marker, eigenes Feld. Eine Zeile, bare UUIDs (kein Label, das steht nur am
# aktuellen Pointer), komma-getrennt, neueste zuerst, gekappt bei 10 (server._retire_session).
# `\s?` statt `:` allein, gleiche Kollisionsfreiheit wie bei @gc-session oben: "@gc-sessions:"
# matcht GC_SESSION_RE NICHT (das 's' vor dem ':' bricht den Match), also keine Zeile fällt
# beiden Regexes gleichzeitig zu.
GC_SESSIONS_RE = re.compile(r"^\s+@gc-sessions:\s?(.*)$")
GC_SESSIONS_LINE_RE = re.compile(r"(?m)^\s+@gc-sessions:")  # für lost-Guard
# Immutable Item-ID = stabile Run-Identität (überlebt Umbenennen/Verschieben, anders
# als der Titel-Fingerprint). @gc-id matcht KEINE andere @gc*-Regex (@gc-i ≠ @gc-re/-done).
GC_ID_RE = re.compile(r"^\s+@gc-id:\s?(.*)$")
GC_ID_LINE_RE = re.compile(r"(?m)^\s+@gc-id:")  # für lost-Guard
# @gc-parent: <id> = Zeiger auf das Eltern-Item (hierarchische Items, Design abgenommen
# 2026-07-23). Sub-Items sind ganz normale FLACHE board.md-Items — die Hierarchie ist eine
# gerenderte/agent-getragene Ansicht über flachem Markdown, KEIN Baum in der Datei
# (Invariante 1). Der Parser hat keine Tiefen-Semantik; echte Verschachtelung hätte
# `lost_id_lines` mit 409 geblockt. Wert = 12-hex-@gc-id des Elternitems.
GC_PARENT_RE = re.compile(r"^\s+@gc-parent:\s?(.*)$")
GC_PARENT_LINE_RE = re.compile(r"(?m)^\s+@gc-parent:")  # für lost-Guard
# @wait: = typisiertes Warte-Feld (Hermes-Übernahme, 2026-07-14 „beides"): worauf das Item
# wartet („alex · !123") + *(Datum)* = gesetzt/zuletzt bestätigt. sweep.py schiebt Waits, die
# WAIT_DECAY_DAYS lang unbestätigt bleiben, zurück nach „Jetzt" — sonst wird die Spalte ein Friedhof.
WAIT_RE = re.compile(r"^\s+@wait:\s?(.*)$")
WAIT_LINE_RE = re.compile(r"(?m)^\s+@wait:")  # für lost-Guard
# @on: = Termin-To-do (Heute-Zone Stufe 1, Item 54fe365c98e4, 21.07.): einmaliges Item,
# das bis zum Stichtag aus der Matrix ausgeblendet bleibt und AM Tag normal erscheint + als
# gestrichelte Termin-Pill in der Heute-Leiste. Vergangenheit = normal sichtbar (überfällig-rot
# wie due). Analog zu @wait geparst/serialisiert — eigener lost-Guard, gleiche Disziplin.
ON_RE = re.compile(r"^\s+@on:\s?(.*)$")
ON_LINE_RE = re.compile(r"(?m)^\s+@on:")  # für lost-Guard
# @done-at: = UTC-ISO-Timestamp, gesetzt vom Frontend beim Abhaken (Owner-Entscheidung 2026-07-15,
# "let's change [retention] to 25h"). sweep.py braucht das für echte Stunden-Präzision —
# das reine `date`-Feld (Tagesgranularität) kann "25h" nicht exakt abbilden. Items ohne
# Stempel (alte/hand-editierte Zeilen) fallen in sweep.py auf Tagesende zurück.
DONE_AT_RE = re.compile(r"^\s+@done-at:\s?(.*)$")
DONE_AT_LINE_RE = re.compile(r"(?m)^\s+@done-at:")  # für lost-Guard
# @gc-last: = Meta des letzten Agent-Runs (Kontextgröße + Zeitpunkt, z.B. "~85k · 2026-07-16 14:32").
# Schreibt der Runner beim Append (2026-07-16, Overlay-Blatt Q3=A: "wie viele token im context"
# + "last edited" in der Overlay-Statuszeile — resume vs. Faden schließen entscheiden können).
GC_LAST_RE = re.compile(r"^\s+@gc-last:\s?(.*)$")
GC_LAST_LINE_RE = re.compile(r"(?m)^\s+@gc-last:")  # für lost-Guard
DATE_RE = re.compile(r"^(.*?)\s*\*\((\d{4}-\d{2}-\d{2})\)\*\s*$")
DUE_RE = re.compile(r"\s*!\((\d{4}-\d{2}-\d{2})\)")
# @stage: = Prozess-Stufen-Historie (append-only wie thread, NICHT Singleton — Q3,
# stage-tags-PLAN.md): eine Zeile pro erreichter Stufe, letzte Zeile = aktueller Stand.
#   @stage: <stufe>[ · <repo-pfad-oder-notiz>] [*(YYYY-MM-DD)*]
# Vokabular (7 Stufen, Q2): plan → rfc → approved → wip → review → tested → deployed.
# `skip:`-Präfix in der Notiz markiert bewusstes Überspringen einer Stufe.
# Failsafe (Design-Prinzip 1): eine Zeile OHNE Stufenwert (`@stage:` leer) ist keine
# Malformung, die crasht oder verschwindet — sie fällt einfach durch zur normalen
# Body-Zeile (siehe _parse_stage). Unbekannte Stufennamen werden geparst, `known: False`.
STAGE_RE = re.compile(r"^\s+@stage:\s?(.*)$")
STAGE_VOCAB = ("plan", "rfc", "approved", "wip", "review", "tested", "deployed")
# Kein STAGE_LINE_RE-Multiline-Zwilling wie bei den anderen lost-Guards (WAIT_LINE_RE
# & Co.): `\s?` nach dem Tag matcht auch `\n`, und ein `(?m)^...\s?(.*)$` über den
# GANZEN Rohtext gefahren frisst dann bei einer leeren `@stage:`-Zeile den Zeilenumbruch
# und liest die Notiz-Gruppe aus der NÄCHSTEN Zeile — genau der malformte Fall, den das
# Failsafe-Prinzip abfangen soll. lost_stage_lines() zählt deshalb pro SPLIT-Zeile
# (wie parse_board selbst), nicht per Multiline-Regex auf dem Fließtext.


# ---------------------------------------------------------------- parsing

def _new_item(done: bool, raw_title: str) -> dict:
    m = DATE_RE.match(raw_title)
    title, date = (m.group(1), m.group(2)) if m else (raw_title, "")
    due = ""
    if dm := DUE_RE.search(title):
        due = dm.group(1)
        title = DUE_RE.sub("", title).strip()
    mark = title.startswith("**") and title.endswith("**") and len(title) > 4
    if mark:
        title = title[2:-2].strip()
    return {"done": done, "title": title, "date": date, "due": due, "mark": mark, "id": "",
            "parent": "", "wait": "", "wait_since": "", "done_at": "", "on": "", "body": [],
            "thread": [], "session": "", "sessions": [], "gc_last": "", "subs": [], "stages": []}


def _parse_sessions(raw: str) -> list[str]:
    """`@gc-sessions:`-Zeileninhalt → Liste abgelöster UUIDs. Komma-getrennt (bewusst
    NICHT " · " — das steckt schon in einzelnen Session-Labels und würde eine Liste
    in Fetzen reißen). Leere/Whitespace-Einträge fallen raus, damit ein Hand-Edit mit
    Trailing-Komma keine Phantom-UUID erzeugt."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _parse_wait(raw: str) -> tuple[str, str]:
    """`alex · !123 *(2026-07-14)*` → ("alex · !123", "2026-07-14").
    Ohne Datums-Suffix (Hand-Edit) bleibt wait_since leer — sweep.py stempelt dann heute."""
    if m := DATE_RE.match(raw):
        return m.group(1).strip(), m.group(2)
    return raw.strip(), ""


def _stage_path(note: str | None) -> str | None:
    """Sieht die Stage-Notiz wie ein Repo-Pfad aus (statt Freitext-Notiz/Skip-Grund)?
    Gleiche Endungs-Logik wie READABLE_SUFFIXES (== Frontend-PATH_RE, index.html:1741) —
    bewusst simpel, kein echter Existenz-Check, nur eine Heuristik für spätere Pill-Links."""
    if not note or note.startswith("skip:"):
        return None
    if "/" not in note:
        return None
    return note if Path(note).suffix.lower() in READABLE_SUFFIXES else None


def _parse_stage(raw: str) -> dict | None:
    """Eine `@stage:`-Zeile parsen (Text NACH dem Tag, z.B. `plan · inbox/analyses/
    stage-tags-PLAN.md *(2026-07-21)*`) zu einem Stage-Event-Dict. `None` bei fehlendem
    Stufenwert (`@stage:` leer/nur Whitespace) — das ist die malformte Zeile aus dem
    Failsafe-Prinzip: der Caller (_attr_line/parse_board) lässt die elif-Kette dann
    einfach zur normalen Body-Zeile durchfallen, statt ein kaputtes Event zu verbuchen.
    `text` = unveränderter Original-Rohtext nach dem Tag — serialize_board rekonstruiert
    die Zeile IMMER darüber, nie aus den geparsten Feldern, damit auch exotische oder
    unbekannte Schreibweisen bytegenau überleben (gleiches Muster wie _thread_event)."""
    text = raw
    dm = DATE_RE.match(raw)
    rest, stage_date = (dm.group(1).strip(), dm.group(2)) if dm else (raw.strip(), None)
    if " · " in rest:
        stage_part, note_part = rest.split(" · ", 1)
        stage_part = stage_part.strip()
        note = note_part.strip() or None
    else:
        stage_part, note = rest, None
    stage = stage_part.lower()
    if not stage:
        return None  # malformt — kein Stufenwert, Caller behandelt als Body-Zeile
    skipped = bool(note) and note.startswith("skip:")
    if skipped:  # der Grund ist die Notiz ohne Präfix; `text` bleibt fürs Serialisieren unberührt
        note = note[len("skip:"):].strip() or None
    return {"stage": stage, "note": note, "path": _stage_path(note), "skipped": skipped,
            "date": stage_date, "known": stage in STAGE_VOCAB, "text": text}


def _thread_event(line: str) -> dict | None:
    """Ordnet eine eingerückte @gc*-Zeile einem Faden-Event zu (exakte Tags)."""
    if m := GC_DONE_RE.match(line):
        return {"kind": "done", "text": m.group(1)}
    if m := GC_REPLY_RE.match(line):
        return {"kind": "reply", "text": m.group(1)}
    if m := GC_ASK_RE.match(line):
        return {"kind": "ask", "text": m.group(1)}
    if m := GC_SYS_RE.match(line):
        return {"kind": "sys", "text": m.group(1)}
    return None


def _attr_line(item: dict, line: str) -> bool:
    """Eingerückte Item-Zeile (Sub/Attribut/Faden-Event/Body) zuordnen — geteilte Kette
    für die Cockpit-Sektion (E3). Spiegelt exakt die board/persons-Ketten in parse_board;
    True = Zeile gehörte zum Item (sonst würde der lost-Guard sie als Verlust zählen)."""
    if m := SUB_RE.match(line):
        item["subs"].append({"done": m.group(1) != " ", "text": m.group(2)})
    elif im := GC_ID_RE.match(line):
        item["id"] = im.group(1).strip()
    elif pm := GC_PARENT_RE.match(line):
        item["parent"] = pm.group(1).strip()
    elif wm := WAIT_RE.match(line):
        item["wait"], item["wait_since"] = _parse_wait(wm.group(1))
    elif dam := DONE_AT_RE.match(line):
        item["done_at"] = dam.group(1).strip()
    elif om := ON_RE.match(line):
        item["on"] = om.group(1).strip()
    elif sm := GC_SESSION_RE.match(line):
        item["session"] = sm.group(1).strip()
    elif ssm := GC_SESSIONS_RE.match(line):
        item["sessions"] = _parse_sessions(ssm.group(1))
    elif gl := GC_LAST_RE.match(line):
        item["gc_last"] = gl.group(1).strip()
    elif (sgm := STAGE_RE.match(line)) and (sev := _parse_stage(sgm.group(1))):
        item["stages"].append(sev)
    elif ev := _thread_event(line):
        item["thread"].append(ev)
    elif line.startswith("  ") and line.strip():
        item["body"].append(line[2:])
    elif line[:1] in (" ", "\t") and line.strip():
        # Wrongly indented (ONE leading space or a tab): falls through every
        # branch above and would silently vanish on the next save — the lost
        # guard only counts known line families, not free text. Failsafe
        # principle: keep it as body rather than silently drop it.
        item["body"].append(line.lstrip())
    else:
        return False
    return True


def parse_board(text: str) -> dict:
    lines = text.split("\n")
    header: list[str] = []
    themes: list[dict] = []
    persons: list[dict] = []
    cockpit: list[dict] = []
    staging: list[dict] = []
    notes: list[str] = []

    section = "header"  # header | board | staging | cockpit | persons | notes
    theme = col = person = item = None

    for line in lines:
        if line.startswith("## ") and section in ("header", "staging"):
            # Auch aus "staging" zurueck in die Matrix: die Staging-Sektion steht UEBER
            # der ersten Themen-Ueberschrift, ohne diesen Ruecksprung wuerde die komplette
            # Matrix in der flachen Staging-Liste verschwinden (Round-Trip-Test 30.07.).
            section = "board"
        if line.strip() == "# Cockpit":
            # Pseudo-Items der Quick Actions / des Tages-Chats (E3/E5): eigene flache
            # Sektion — taucht dadurch in KEINER Themen-Matrix/Personen-Liste auf und
            # bleibt vom Sweep (iteriert themes/persons) unberührt.
            section, item = "cockpit", None
            continue
        if line.strip() == "# Staging":
            # Vorschlaege, die ein Agent unaufgefordert erzeugt hat (Faden e5bb9b10d7eb,
            # 2026-07-30). Flache Sektion wie Cockpit: taucht in KEINER Themen-Matrix auf
            # und bleibt vom Sweep unberuehrt — hier gilt bewusst keine Verfallsregel.
            section, item = "staging", None
            continue
        if line.startswith("# ") and section_key(line[2:]) == "Personen":
            section, item, person = "persons", None, None
            continue
        if line.strip() in _NOTES_HEADS:
            section, item = "notes", None
            continue
        if section == "notes":
            notes.append(line)
            continue

        if section == "header":
            header.append(line)
            continue

        if section == "board":
            if line.startswith("## "):
                theme = {"name": line[3:].strip(), "cols": {c: [] for c in DEFAULT_COLUMNS}}
                themes.append(theme)
                col, item = None, None
            elif line.startswith("### "):
                name = line[4:].strip()
                col = column_key(name)
                if col:
                    theme["cols"].setdefault(col, [])   # Extra-Spalte (z.B. "Wartet auf andere")
                item = None
            elif (m := ITEM_RE.match(line)) and theme and col:
                item = _new_item(m.group(1) != " ", m.group(2))
                theme["cols"][col].append(item)
            elif item and (m := SUB_RE.match(line)):
                item["subs"].append({"done": m.group(1) != " ", "text": m.group(2)})
            elif item and (im := GC_ID_RE.match(line)):
                item["id"] = im.group(1).strip()
            elif item and (pm := GC_PARENT_RE.match(line)):
                item["parent"] = pm.group(1).strip()
            elif item and (wm := WAIT_RE.match(line)):
                item["wait"], item["wait_since"] = _parse_wait(wm.group(1))
            elif item and (dam := DONE_AT_RE.match(line)):
                item["done_at"] = dam.group(1).strip()
            elif item and (om := ON_RE.match(line)):
                item["on"] = om.group(1).strip()
            elif item and (sm := GC_SESSION_RE.match(line)):
                item["session"] = sm.group(1).strip()
            elif item and (ssm := GC_SESSIONS_RE.match(line)):
                item["sessions"] = _parse_sessions(ssm.group(1))
            elif item and (gl := GC_LAST_RE.match(line)):
                item["gc_last"] = gl.group(1).strip()
            elif item and (sgm := STAGE_RE.match(line)) and (sev := _parse_stage(sgm.group(1))):
                item["stages"].append(sev)
            elif item and (ev := _thread_event(line)):
                item["thread"].append(ev)
            elif item and line.startswith("  ") and line.strip():
                item["body"].append(line[2:])
            elif item and line[:1] in (" ", "\t") and line.strip():
                # Failsafe as in _attr_line(): wrongly indented lines stay as
                # body instead of vanishing on the next write.
                item["body"].append(line.lstrip())
        elif section in ("cockpit", "staging"):
            if m := ITEM_RE.match(line):
                item = _new_item(m.group(1) != " ", m.group(2))
                (cockpit if section == "cockpit" else staging).append(item)
            elif item and _attr_line(item, line):
                pass
        else:  # persons
            if line.startswith("## "):
                name, _, link = line[3:].partition(" → ")
                name = name.strip()
                person = {"name": name, "link": link.strip(), "items": []}
                if name.startswith(MEETING_MARK):
                    person["name"] = name[len(MEETING_MARK):].strip()
                    person["kind"] = "meeting"
                persons.append(person)
                item = None
            elif (m := ITEM_RE.match(line)) and person is not None:
                item = _new_item(m.group(1) != " ", m.group(2))
                person["items"].append(item)
            elif item and (m := SUB_RE.match(line)):
                item["subs"].append({"done": m.group(1) != " ", "text": m.group(2)})
            elif item and (im := GC_ID_RE.match(line)):
                item["id"] = im.group(1).strip()
            elif item and (pm := GC_PARENT_RE.match(line)):
                item["parent"] = pm.group(1).strip()
            elif item and (wm := WAIT_RE.match(line)):
                item["wait"], item["wait_since"] = _parse_wait(wm.group(1))
            elif item and (dam := DONE_AT_RE.match(line)):
                item["done_at"] = dam.group(1).strip()
            elif item and (om := ON_RE.match(line)):
                item["on"] = om.group(1).strip()
            elif item and (sm := GC_SESSION_RE.match(line)):
                item["session"] = sm.group(1).strip()
            elif item and (ssm := GC_SESSIONS_RE.match(line)):
                item["sessions"] = _parse_sessions(ssm.group(1))
            elif item and (gl := GC_LAST_RE.match(line)):
                item["gc_last"] = gl.group(1).strip()
            elif item and (sgm := STAGE_RE.match(line)) and (sev := _parse_stage(sgm.group(1))):
                item["stages"].append(sev)
            elif item and (ev := _thread_event(line)):
                item["thread"].append(ev)
            elif item and line.startswith("  ") and line.strip():
                item["body"].append(line[2:])
            elif item and line[:1] in (" ", "\t") and line.strip():
                # Failsafe as in _attr_line(): wrongly indented lines stay as
                # body instead of vanishing on the next write.
                item["body"].append(line.lstrip())

    while header and not header[-1].strip():
        header.pop()
    while notes and not notes[0].strip():
        notes.pop(0)
    while notes and not notes[-1].strip():
        notes.pop()
    return {"header": header, "themes": themes, "staging": staging,
            "cockpit": cockpit, "persons": persons, "notes": notes}


def _migrate_legacy_gc(board: dict) -> None:
    """Defensiv: ein veraltetes UI-Tab (vor dem thread-Umbau) schickt beim Save noch
    `gc: [str]` statt `thread: [event]`. Ohne Migration ließe serialize_board diese
    Kommentare fallen (genau der Clobber vom 2026-07-08). Hier gc→thread(ask) retten,
    bevor geschrieben wird. Läuft nur, wenn `gc` vorhanden und `thread` leer ist."""
    def fix(it: dict) -> None:
        if it.get("gc") and not it.get("thread"):
            it["thread"] = [{"kind": "ask", "text": g} for g in it["gc"]]
        it.pop("gc", None)
    for th in board.get("themes", []):
        for c in theme_cols(th):
            for it in th["cols"].get(c, []):
                fix(it)
    for p in board.get("persons", []):
        for it in p.get("items", []):
            fix(it)


def item_lines(it: dict) -> list[str]:
    """Die Markdown-Zeilen EINES Items — ohne Leerzeile dahinter.

    Bewusst als Top-Level-Funktion (vorher eine Closure in `serialize_board`): der
    chirurgische Append-Pfad (`_gc_append`) baut damit denselben Block, statt die
    Einrückungs-Grammatik von Hand nachzubilden. Eine Quelle, ein Format — sonst
    driftet der Zweitweg genau dann ab, wenn er gebraucht wird."""
    out: list[str] = []
    mark = "x" if it["done"] else " "
    date = f" *({it['date']})*" if it.get("date") else ""
    due = f" !({it['due']})" if it.get("due") else ""
    title = f"**{it['title']}**" if it.get("mark") else it["title"]
    out.append(f"- [{mark}] {title}{due}{date}")
    for b in it.get("body", []):
        out.append(f"  {b}")
    for sev in it.get("stages", []):
        out.append(f"  @stage: {sev.get('text', '')}".rstrip())
    if it.get("id"):
        out.append(f"  @gc-id: {it['id']}")
    if it.get("parent"):
        out.append(f"  @gc-parent: {it['parent']}")
    if it.get("wait") or it.get("wait_since"):
        since = f"*({it['wait_since']})*" if it.get("wait_since") else ""
        out.append("  @wait: " + " ".join(p for p in (it.get("wait", ""), since) if p))
    if it.get("done_at"):
        out.append(f"  @done-at: {it['done_at']}")
    if it.get("on"):
        out.append(f"  @on: {it['on']}")
    for ev in it.get("thread", []):
        out.append(f"  {GC_TAG[ev['kind']]} {ev.get('text', '')}".rstrip())
    if it.get("session"):
        out.append(f"  @gc-session: {it['session']}")
    if it.get("sessions"):
        out.append(f"  @gc-sessions: {', '.join(it['sessions'])}")
    if it.get("gc_last"):
        out.append(f"  @gc-last: {it['gc_last']}")
    for s in it.get("subs", []):
        out.append(f"  - [{'x' if s['done'] else ' '}] {s['text']}")
    return out


def serialize_board(board: dict) -> str:
    out: list[str] = list(board["header"]) + [""]

    def emit_item(it: dict) -> None:
        out.extend(item_lines(it))
        out.append("")

    if board.get("staging"):
        # Ueber der Matrix — Owner-Wunsch (Blatt 2, Q2=C): Staging soll man sehen,
        # ohne zu scrollen. Nur bei Inhalt emittiert, sonst bekaemen Boards ohne
        # Vorschlaege eine leere Sektion aufgezwungen (Round-Trip bleibt wortgleich).
        out += ["# Staging", ""]
        for it in board["staging"]:
            emit_item(it)

    for theme in board["themes"]:
        out += [f"## {theme['name']}", ""]
        for col in theme_cols(theme):
            out += [f"### {COLUMN_FILE_NAMES[col]}", ""]
            for it in theme["cols"].get(col, []):
                emit_item(it)

    if board.get("cockpit"):
        # Nur emittieren, wenn Items da sind — Boards ohne Quick-Action-Historie
        # bekommen keine leere Sektion aufgezwungen (Round-Trip bleibt wortgleich).
        out += ["# Cockpit", ""]
        for it in board["cockpit"]:
            emit_item(it)

    out += [f"# {SECTION_FILE_NAMES['Personen']}", ""]
    for p in board["persons"]:
        name = (MEETING_MARK + p["name"]) if p.get("kind") == "meeting" else p["name"]
        head = f"## {name}" + (f" → {p['link']}" if p.get("link") else "")
        out += [head, ""]
        for it in p["items"]:
            emit_item(it)

    out += [f"# {SECTION_FILE_NAMES['Notizen']}", ""]
    out += board.get("notes", [])

    # collapse >1 blank lines, single trailing newline
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return text.strip("\n") + "\n"


# ---------------------------------------------------------------- server

def file_etag(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def text_etag(text: str) -> str:
    """ETag aus GENAU dem gelesenen Text — Board+ETag als konsistentes Paar.
    (file_etag nach separatem read_text wäre ein zweiter Read: ein gc-append
    dazwischen und der Client bekäme Board A mit ETag B → sein nächster Save
    überschriebe die frische Agent-Antwort. SOL-Finding 2026-07-12.)"""
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def item_body_etag(body: list[str]) -> str:
    """Revision of an item body for optimistic, item-local writes.

    A thread append is commutative: the server reads fresh and appends. A
    whole-body replace is not — a long agent run could otherwise overwrite a
    body the owner edited after the prompt was built. The revision therefore
    counts only the opaque body lines, not stage/thread/meta.
    """
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Entscheidungsblatt am Faden (2026-08-11, Blatt f9eea278d2a1 Q1/Q3=A) ──────────
# Trägt der LETZTE Faden-Turn einen .html-Pfad, gilt: „zu diesem Item liegt ein Blatt vor".
# Bewusst nur der letzte Turn — Owner: „wenn es vor 2 turns war, soll es nicht per marker
# gezeigt werden". Damit verschwindet das Zeichen von selbst, sobald er geantwortet hat,
# und es braucht KEINE eigene Marker-Zeile in board.md (die griffe nur für neue Blätter).
# Lange Turns liegen als Sidecar-Einzeiler in board.md — der Pfad steht dann erst im
# Volltext, also hier expandieren, sonst sieht der Server bei genau den Agent-Antworten
# nichts, um die es geht.
SHEET_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.html)(?![\w])")


def item_sheet(item: dict) -> str:
    """Repo-relativer Pfad des Entscheidungsblatts am letzten Turn — oder "".
    Nur was /repo-file/ auch ausliefern würde (kein Dot-Segment, existiert, unter GC_ROOT)."""
    turns = [ev for ev in item.get("thread", []) if ev.get("kind") in ("ask", "reply")]
    if not turns:
        return ""
    text = turns[-1].get("text", "")
    text = sidecar.expand(text) or text
    # Absolute Pfade unters Repo-Root vorab relativieren: der Lookbehind von SHEET_RE
    # verhindert Treffer mitten im Pfad — und damit JEDEN absoluten Pfad. Ein absolut
    # verlinktes Blatt war so unsichtbar (Regression 14.08., zweimal am selben Tag);
    # der Einzelpatch damals fixte nur den Link, nicht die Ursache.
    text = text.replace(f"{GC_ROOT}/", "")
    for rel in reversed(SHEET_RE.findall(text)):
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            continue
        p = (GC_ROOT / rel).resolve()
        if p.is_relative_to(GC_ROOT) and p.is_file():
            return rel
    return ""


def sheet_kind(rel: str) -> str:
    """"sheet" (decision sheet) or "demo" (click-through) — or "" without a file.

    A finished feature can grow more than a sheet: a click-through demo (same
    mechanism, different purpose) can sit alongside it. A demo asks NOTHING, so it
    must not put the card into "waiting on the owner" or show "Decision sheet" in
    the split pane. Detected by path, not content: `demos/` in the path or a name
    ending in `-demo.html`."""
    if not rel:
        return ""
    parts = rel.split("/")
    return "demo" if "demos" in parts[:-1] or parts[-1].endswith("-demo.html") else "sheet"


def item_needs_input(item: dict) -> str:
    """Wartet der letzte Faden-Turn auf den Input des Owners? "" | "sheet" | "handoff" | "frage".

    Needs-Input (17.08., Blatt auto-run-needs-input Q1=A): drei Signale, alle nur am
    LETZTEN Nicht-sys-Turn und nur wenn der eine Agenten-Antwort ist — damit löscht sich
    der Zustand von selbst, sobald der Owner antwortet (gleiche Logik wie item_sheet, keine
    Marker-Zeile in board.md). Blatt und 🔑 sind strukturell; die Klartext-Frage braucht
    den ❓-Marker aus dem Kontrakt (verzeihend wie die 🔑-Konvention: vergessener Marker
    = normale Antwort, kein Fehler)."""
    t = [e for e in item.get("thread", []) if e.get("kind") != "sys"]
    if not t or t[-1].get("kind") != "reply":
        return ""
    if sheet_kind(item_sheet(item)) == "sheet":
        return "sheet"
    text = t[-1].get("text", "")
    text = sidecar.expand(text) or text
    first = text.lstrip().split("\n", 1)[0]
    if "🔑" in first and "CLI-Handoff" in first:
        return "handoff"
    if "❓" in first:
        return "frage"
    return ""


def item_awaiting_cut(item: dict) -> bool:
    """Wartet die letzte Runde darauf, dass der Owner sie abhakt?

    „Abhaken" IST der Fadenschnitt: derselbe `@gc-done:`-Turn, den `✂ New thread` im
    Overlay schreibt. Deshalb KEIN neues Zustandsfeld — abgeleitet wie needs_input,
    nichts davon steht in board.md. Regel: steht nach der letzten Antwort kein `done`,
    wartet die Runde auf den Haken; gab es noch gar keine Antwort (nie gelaufen, oder
    Lauf ohne Antwort), gibt es nichts abzuhaken.

    `gc_runner.session_cut` ist bewusst importiert statt nachgebaut: derselbe Test
    entscheidet, ob der nächste Run frisch statt per --resume startet. Kartenzustand und
    Session-Verhalten dürfen nicht auseinanderlaufen — „abgehakt" heißt genau
    „nächste Runde beginnt bei Null"."""
    import gc_runner
    t = item.get("thread", [])
    if not any(e.get("kind") == "reply" for e in t):
        return False
    return not gc_runner.session_cut(t)


def annotate_sheets(board: dict) -> None:
    """Anzeige-Feld, KEIN board.md-Inhalt: wird nur in die /api/board-Antwort gehängt.
    parse_board bleibt rein (Round-Trip-Invariante), item_lines ignoriert die Zusatzkeys."""
    for _, _, _, it in _all_items(board):
        it["sheet"] = item_sheet(it)
        it["sheet_kind"] = sheet_kind(it["sheet"])
        it["needs_input"] = item_needs_input(it)
        it["awaiting_cut"] = item_awaiting_cut(it)


def _believable(stamps: list[str | None]) -> list[str | None]:
    """Filtert die Zeitstempel eines Fadens auf die längste widerspruchsfreie Kette.

    Ein Faden läuft vorwärts: Turn N+1 kann nicht VOR Turn N liegen. Wo die Reihenfolge
    bricht, stammt die Sidecar-Datei nicht aus dem Turn — die board.md-Diät hat am
    2026-07-17 Altbestand nachträglich ausgelagert (24 Dateien in EINER Minute), und
    diese Dateien tragen die Uhrzeit der MIGRATION im Namen. Ungefiltert behauptete die
    Anzeige dann, die Frage des Owners sei zwei Tage nach der Antwort darauf gestellt worden.

    Warum die längste Kette und nicht „ab dem ersten Bruch abschneiden": ein einzelner
    falscher Stempel am Anfang würde sonst den ganzen echten Rest verwerfen. Nicht
    erkennbar sind Migrationsstempel, die zufällig chronologisch passen — die sind dann
    aber auch harmlos. Im Zweifel lieber keine Zeit als eine erfundene.
    """
    idx = [i for i, s in enumerate(stamps) if s]
    if len(idx) < 2:
        return stamps
    best = [1] * len(idx)          # längste Kette, die bei idx[k] endet
    prev: list[int | None] = [None] * len(idx)
    for k in range(len(idx)):
        for j in range(k):
            if stamps[idx[j]] <= stamps[idx[k]] and best[j] + 1 > best[k]:
                best[k], prev[k] = best[j] + 1, j
    # Gleichstand → die Kette, die am WEITESTEN HINTEN endet. Neue Turns schreibt immer
    # der Live-Pfad (Server-Append/Runner) und der stempelt korrekt; nachträglich erzeugte
    # Dateien liegen im Altbestand. Bei gleicher Länge ist die jüngere Kette die echtere.
    k: int | None = max(range(len(idx)), key=lambda i: (best[i], i))
    keep = set()
    while k is not None:
        keep.add(idx[k])
        k = prev[k]
    return [s if i in keep else None for i, s in enumerate(stamps)]


def annotate_turn_times(board: dict) -> None:
    """Anzeige-Feld `at` pro Faden-Turn, KEIN board.md-Inhalt (wie annotate_sheets).

    Der Faden liest sich wie ein Chat, hatte aber keine Uhrzeit — man sah nicht, ob
    zwischen zwei Turns fünf Minuten oder ein Tag lagen. Genau dieser Abstand erklärt
    im Nachhinein, warum ein Run kalt startete: die Cache-Pill zeigt nur den JETZT-
    Zustand, die Historie war blind (2026-08-13).

    Quelle ist der Sidecar-Dateiname, kein neues Feld: dadurch gilt die Zeit rückwirkend
    für den gesamten Bestand und die Zeilen-Serialisierung bleibt unberührt. Preis der
    Ehrlichkeit: kurze Turns ohne Sidecar bleiben ohne Zeit.
    """
    for _, _, _, it in _all_items(board):
        thread = it.get("thread", [])
        for ev, at in zip(thread, _believable([sidecar.turn_time(e.get("text", "")) for e in thread])):
            if at:
                ev["at"] = at


def _claude_cross_run(rec: dict) -> dict | None:
    """Cross-Run-Cachebilanz eines Claude-Runs aus den Turn-1-Feldern des Usage-Logs.

    Turn 1 ist der einzige ehrliche Messpunkt (siehe ``gc_runner._erster_turn_cache``):
    was dort GELESEN wird, kam über die Run-Grenze aus dem Cache; was GESCHRIEBEN wird,
    musste neu eingelesen werden. Das run-weite ``cache_hit_pct`` liegt dagegen fast immer
    bei ~96 %, weil jeder Werkzeug-Turn den bisherigen Verlauf wieder liest — es misst
    Within-Run und hat uns 2026-08 einmal falsche Entwarnung gegeben.

    ``t1_input`` gibt es erst seit 2026-08-13; alte Zeilen fallen sauber auf read+write
    zurück, statt hier zu verschwinden.
    """
    read, write = rec.get("t1_read"), rec.get("t1_write")
    if read is None or write is None:
        return None
    gesamt = int(read) + int(write) + int(rec.get("t1_input") or 0)
    if not gesamt:
        return None
    return {"ts": rec.get("ts"), "cross_run_input_tokens": gesamt,
            "cross_run_cache_read": int(read),
            "cross_run_cache_hit_pct": round(100 * int(read) / gesamt),
            "context_source": "claude-turn1", "ttl": rec.get("ttl")}


def annotate_cross_run_cache(board: dict) -> None:
    """Letzte gemessene Resume-Cachebilanz als flüchtiges UI-Feld anhängen.

    Quelle bleibt das append-only Usage-Log; ``board.md`` bekommt keine zweite
    Telemetrie-Wahrheit. Nur Resume-Läufe sind Cross-Run-Messungen — ein Fresh-Run kann
    zwar gecachte statische Präfixe lesen, sagt aber nichts über diesen Faden aus.

    Beide Runner landen im selben Feld, aber auf verschiedenen Wegen: Codex liefert die
    Bilanz fertig aus seiner Rollout-Datei, für Claude rechnen wir sie aus Turn 1.
    """
    import gc_runner
    latest: dict[str, dict] = {}
    # Jeder Codex-Lauf, auch der nicht-resumte: Codex teilt seinen Prefix-Cache
    # prozessübergreifend, also verdrängt ihn JEDER fremde Lauf — nicht nur ein resumter.
    codex_runs: list[tuple[str, str]] = []
    try:
        with open(gc_runner.USAGE_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                gid = str(rec.get("gc_id") or "")
                is_codex = str(rec.get("model") or "").startswith("codex")
                if is_codex and rec.get("ts"):
                    codex_runs.append((str(rec["ts"]), gid))
                if not gid or not rec.get("resumed"):
                    continue
                if is_codex:
                    if not rec.get("cross_run_input_tokens"):
                        continue
                    obs = {k: rec.get(k) for k in (
                        "ts", "cross_run_input_tokens", "cross_run_cache_read",
                        "cross_run_cache_hit_pct", "context_source")}
                else:
                    obs = _claude_cross_run(rec)
                    if not obs:
                        continue
                latest[gid] = obs
    except OSError:
        return
    for _, _, _, it in _all_items(board):
        if it.get("id") not in latest:
            continue
        obs = latest[it["id"]]
        # Verdrängungsdruck statt Restzeit: bei Codex bricht die Trefferquote nicht mit der
        # Uhr, sondern mit fremden Läufen dazwischen. Die Zahl ist deshalb die zweite
        # Hälfte der Aussage; die Uhr allein wäre zu optimistisch.
        if obs.get("context_source") == "codex-rollout" and obs.get("ts"):
            obs = dict(obs)
            obs["codex_runs_since"] = sum(
                1 for ts, gid in codex_runs if ts > obs["ts"] and gid != it["id"])
        it["cache_observation"] = obs


def _all_items(board: dict):
    for th, c in _all_cols(board):
        for it in th["cols"][c]:
            yield ("theme", th["name"], c, it)
    for it in board.get("staging", []):
        yield ("staging", "Staging", None, it)
    for it in board.get("cockpit", []):
        yield ("cockpit", "Cockpit", None, it)
    for p in board["persons"]:
        for it in p["items"]:
            yield ("person", p["name"], None, it)


# Arbeitsstand-Aufräumen (2026-07-22, Blatt „Arbeitsstand" Q3=A). Der
# `### Arbeitsstand`-Block im Item-Body ist Arbeitsspeicher für einen LAUFENDEN Faden:
# er kauft dem Agenten Mut zum Faden-Schnitt. An einem abgehakten Item ist er nur noch
# Ballast — und beim Wiederaufgreifen sogar irreführend, weil er einen längst überholten
# Stand behauptet. git trägt die Historie, also darf er beim Abhaken weg.
# Bewusst NICHT im Agent-Kontrakt gelöst (Option B im Blatt): eine Regel, die nur beim
# Abschließen greift, wird zuverlässig vergessen — beim 200k-Hinweis ist genau das passiert.
ARBEITSSTAND_ARCHIV = GC_ROOT / "logs" / "dreaming" / "arbeitsstand-archiv.md"
# Existing boards used the German heading; the English product contract emits the
# new heading.  Accept both forever so a language pass never becomes a data migration.
ARBEITSSTAND_HEAD_RE = re.compile(
    r"^\s*#{1,6}\s*(?:Arbeitsstand|Working state)\s*:?\s*$", re.IGNORECASE
)
_BODY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")


def strip_arbeitsstand(body: list[str]) -> list[str]:
    """Body ohne den Arbeitsstand-Block (Kopfzeile bis zur nächsten Überschrift oder
    Body-Ende). Bleibt danach nur noch der ···-Marker am Ende übrig, fliegt der mit —
    sonst zeigt das Overlay ein leeres Deep-Dive-Fach. Ohne Block: Body unverändert."""
    out: list[str] = []
    skipping = found = False
    for line in body:
        if ARBEITSSTAND_HEAD_RE.match(line):
            skipping = found = True
            continue
        if skipping:
            if not _BODY_HEADING_RE.match(line):
                continue
            skipping = False
        out.append(line)
    if not found:
        return body
    while out and out[-1].strip() in ("", "···"):
        out.pop()
    return out


def extract_arbeitsstand(body: list[str]) -> list[str]:
    """Nur der Inhalt des Arbeitsstand-Blocks (ohne Kopfzeile). Gegenstück zu
    strip_arbeitsstand: was das eine wegwirft, gibt das andere zurück."""
    out: list[str] = []
    skipping = False
    for line in body:
        if ARBEITSSTAND_HEAD_RE.match(line):
            skipping = True
            continue
        if skipping:
            if not _BODY_HEADING_RE.match(line):
                out.append(line)
                continue
            skipping = False
    while out and out[-1].strip() in ("", "···"):
        out.pop()
    return out


def archive_arbeitsstand(title: str, gc_id: str, block: list[str]) -> bool:
    """Den Arbeitsstand beim Abhaken ins Rohlager kippen statt wegwerfen.

    Warum (Memory-Rückfluss, 27.07.): Der Arbeitsstand ist der einzige Ort, an
    dem verworfene Optionen und `Gelernt:`-Zeilen stehen — beim Abhaken starb das
    bisher ersatzlos. Der Pilot-Lauf hat gezeigt, dass genau diese Blöcke der
    dichteste Rohstoff für die Muster-Erkennung sind. Bewusst NUR mechanisches
    Wegschreiben: beim Abhaken läuft kein Agent, die Disposition macht der Kurator
    später. Archivieren darf das Speichern nie blockieren — daher best effort."""
    if not block:
        return False
    try:
        ARBEITSSTAND_ARCHIV.parent.mkdir(parents=True, exist_ok=True)
        new = not ARBEITSSTAND_ARCHIV.exists()
        with ARBEITSSTAND_ARCHIV.open("a", encoding="utf-8") as fh:
            if new:
                fh.write(
                    "# Arbeitsstand-Rohlager\n\n"
                    "Arbeitsstände abgehakter Items, mechanisch beim Abhaken gesichert "
                    "(`archive_arbeitsstand`). Rohstoff für den Dreaming-/Kurator-Lauf — "
                    "kein Nachschlagewerk. Der Kurator darf hier verdaute Einträge kürzen.\n")
            fh.write(f"\n## {title.strip() or '(ohne Titel)'}\n")
            fh.write(f"*abgehakt {datetime.now():%Y-%m-%d %H:%M} · @gc-id "
                     f"{gc_id or '—'}*\n\n")
            fh.write("\n".join(ln.rstrip() for ln in block) + "\n")
        return True
    except OSError:
        return False


def drop_arbeitsstand_on_done(disk: dict, incoming: dict) -> int:
    """Items, die mit diesem Save von offen auf erledigt kippen, verlieren ihren
    Arbeitsstand — vorher wandert er ins Rohlager (siehe archive_arbeitsstand).
    Nur dieser Übergang — ein Re-Save eines längst erledigten Items fasst nichts
    an, sonst würde ein Hand-Edit am Body still wieder wegradiert."""
    was_open = {it["id"] for _s, _n, _c, it in _all_items(disk)
                if it.get("id") and not it.get("done")}
    hits = 0
    for _s, _n, _c, it in _all_items(incoming):
        if it.get("done") and it.get("id") in was_open and it.get("body"):
            stripped = strip_arbeitsstand(it["body"])
            if stripped is not it["body"]:
                archive_arbeitsstand(it.get("title", ""), it.get("id", ""),
                                     extract_arbeitsstand(it["body"]))
                it["body"] = stripped
                hits += 1
    return hits


def guard_scope(text: str, board: dict) -> str:
    """Der Textbereich, in dem ein Save überhaupt etwas vernichten KANN.

    Bisher hat jeder lost_*-Guard schlicht alles vor `# Notizen` gezählt. Damit war der
    HEADER mit drin — die Zeilen vor der ersten `## `-Überschrift. Die reicht
    serialize_board aber wörtlich durch (`board["header"]`), da geht nichts verloren.

    Folge war ein Fehlalarm mit maximalem Schaden: eine einzelne Checkbox oder
    `@gc-id:`-Zeile im Header (jemand tippt ein To-do über die erste Überschrift) zählte
    als „verlorene Zeile" und sperrte damit JEDEN Schreibpfad des Boards — neues Item,
    Agent-Run, Faden-Antwort, Chat, Capture. Dauerhaft, denn der Zustand heilt nicht von
    selbst, und bis zum 28.07. sagte nichts im UI, welche Zeile gemeint war.

    Nachgewiesen (28.07.): `- [ ] x` über der ersten Überschrift → `lost_total` = 1,
    obwohl parse→serialize die Zeile nachweislich unverändert zurückschreibt.

    Untere Grenze bleibt die Notizen-Sektion (Freitext, ebenfalls verbatim) — in JEDER
    Schreibweise, die der Parser akzeptiert (`# Notizen` legacy, `# Notes` on-disk).
    Der Schnitt oben kommt aus dem Parse selbst statt aus einer zweiten Regel — die
    Header-Grenze wäre sonst an zwei Stellen definiert und würde auseinanderlaufen."""
    body = text
    for head in _NOTES_HEADS:
        body = body.split(f"\n{head}")[0]
    header = board.get("header") or []
    return "\n".join(body.split("\n")[len(header):]) if header else body


def lost_boxes(text: str, board: dict) -> int:
    """Checkbox-Zeilen im Rohtext (vor # Notizen), die der Parser NICHT als
    Item/Sub erkannt hat. >0 heißt: ein Save würde diese Zeilen still vernichten."""
    raw = len(BOX_RE.findall(guard_scope(text, board)))
    parsed = sum(1 + len(it["subs"]) for _s, _n, _c, it in _all_items(board))
    return max(0, raw - parsed)


def lost_thread_events(text: str, board: dict) -> int:
    """@gc*-Zeilen im Rohtext, die der Parser NICHT als Faden-Event erkannt hat.
    Analog zu lost_boxes: >0 heißt, ein Save würde Faden-Turns still vernichten."""
    raw = len(THREAD_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(len(it["thread"]) for _s, _n, _c, it in _all_items(board))
    return max(0, raw - parsed)


def find_item(board: dict, addr: dict) -> list[dict]:
    """Item über einen Fingerprint adressieren (statt fragilem Array-Index):
    Scope + Sektionsname + (Spalte) + Titel + Datum. Gibt alle Treffer zurück;
    der Caller behandelt !=1 als Konflikt (409). Mit `id` im addr: eindeutig per
    immutabler ID (überlebt Umbenennen/Verschieben) — sonst Fallback auf Fingerprint."""
    if item_id := addr.get("id"):
        return [it for _s, _n, _c, it in _all_items(board) if it.get("id") == item_id]
    scope = addr.get("scope")
    name = addr.get("name")
    title = addr.get("title")
    date = addr.get("date", "")
    col = addr.get("col")
    out = []
    for s, sname, scol, it in _all_items(board):
        if s != scope or sname != name:
            continue
        if scope == "theme" and col in KNOWN_COLUMNS and scol != col:
            continue
        if it["title"] == title and it.get("date", "") == date:
            out.append(it)
    return out


# --- chirurgischer Schreibpfad -------------------------------------------------
# Warum es das gibt (Vorfall 28.07.2026): jeder Append serialisiert heute die GANZE
# board.md, deshalb MUSS der lost-Guard global prüfen — und eine kaputte Zeile an
# Item A sperrte die Agent-Antwort an Item Z gleich mit. Die Helfer hier erlauben,
# NUR den Zeilenblock eines Items zu ersetzen; alles davor und dahinter bleibt
# byteidentisch, der Guard darf sich damit auf das betroffene Item beschränken.

# Bewusst dieselben Sektionsmarken wie parse_board — der Rohtext-Scan darf keine
# zweite Wahrheit über "wo fängt die Matrix an" aufmachen.
_SECTION_HEADS = ("# Cockpit", "# Staging", "# Personen",
                  f"# {SECTION_FILE_NAMES['Personen']}") + tuple(_NOTES_HEADS)


def _block_window(lines: list[str]) -> tuple[int, int]:
    """Zeilenbereich, in dem überhaupt Items stehen können: ab der ersten
    Sektionsüberschrift bis zur Notizen-Sektion (legacy `# Notizen` oder on-disk
    `# Notes`). Der Header darf `- [ ]`-Zeilen enthalten, ohne dass sie als Item
    gelten (genau die Falle vom 28.07.)."""
    lo, hi = len(lines), len(lines)
    for i, l in enumerate(lines):
        if l.startswith("## ") or l.strip() in _SECTION_HEADS:
            lo = i
            break
    for i, l in enumerate(lines):
        if l.strip() in _NOTES_HEADS:
            hi = i
            break
    return lo, hi


def raw_item_blocks(raw: str) -> list[tuple[int, int]]:
    """Zeilenspannen `(start, end)` aller Top-Level-Items im Rohtext.
    Ein Block endet am nächsten Item, an der nächsten Überschrift oder am
    Dateiende — Leerzeilen dahinter gehören nicht dazu."""
    lines = raw.split("\n")
    lo, hi = _block_window(lines)
    out: list[tuple[int, int]] = []
    for s in range(lo, hi):
        if not ITEM_RE.match(lines[s]):
            continue
        e = hi
        for j in range(s + 1, hi):
            if ITEM_RE.match(lines[j]) or lines[j].startswith("#"):
                e = j
                break
        while e > s + 1 and not lines[e - 1].strip():
            e -= 1
        out.append((s, e))
    return out


def _parse_block(block: list[str]) -> dict | None:
    """Einen Item-Block für sich parsen — in ein Minimal-Board eingebettet, damit
    exakt dieselbe Grammatik wie im Vollparse gilt. `None`, wenn dabei nicht genau
    ein Item herauskommt."""
    mini = parse_board("\n".join(["## _", "", "### Jetzt", ""] + block) + "\n")
    items = [it for _s, _n, _c, it in _all_items(mini)]
    return items[0] if len(items) == 1 else None


def locate_item_block(raw: str, addr: dict) -> tuple[int, int, dict] | None:
    """Zielitem im ROHTEXT adressieren: `(start, end, geparstes_item)`.

    `None`, wenn es nicht genau einen Treffer gibt. Ohne `id` im addr wird über
    Titel+Datum gematcht — bewusst strenger als `find_item` (das zusätzlich über
    Thema/Spalte disambiguiert, was ein Einzelblock nicht wissen kann): lieber ein
    ehrliches 409 als der falsche Block."""
    lines = raw.split("\n")
    hits: list[tuple[int, int, dict]] = []
    for s, e in raw_item_blocks(raw):
        it = _parse_block(lines[s:e])
        if it is None:
            continue
        if item_id := addr.get("id"):
            ok = it.get("id") == item_id
        else:
            ok = (it["title"] == addr.get("title")
                  and it.get("date", "") == addr.get("date", ""))
        if ok:
            hits.append((s, e, it))
    return hits[0] if len(hits) == 1 else None


def splice_item_block(raw: str, start: int, end: int, it: dict) -> str:
    """Den Block `[start, end)` durch die neu emittierten Zeilen von `it` ersetzen.
    Alles außerhalb bleibt Byte für Byte stehen — auch ungeparste Zeilen an anderen
    Items, die ein `serialize_board()` still verschluckt hätte."""
    lines = raw.split("\n")
    lines[start:end] = item_lines(it)
    return "\n".join(lines)


def lost_session_lines(text: str, board: dict) -> int:
    """@gc-session:-Zeilen im Rohtext, die der Parser NICHT als Session-Pointer
    erkannt hat. Schützt den Resume-Pointer vor stillem Verlust (wie lost_boxes)."""
    raw = len(GC_SESSION_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("session"))
    return max(0, raw - parsed)


def lost_sessions_lines(text: str, board: dict) -> int:
    """@gc-sessions:-Zeilen (Verlauf abgelöster Resume-Pointer), die der Parser nicht
    erkannt hat (z.B. >1 je Item) → blockt Save, statt die Rückblätter-Historie still
    zu verlieren. Gleiche Disziplin wie lost_session_lines für den Singular-Pointer."""
    raw = len(GC_SESSIONS_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("sessions"))
    return max(0, raw - parsed)


def lost_gc_last_lines(text: str, board: dict) -> int:
    """@gc-last:-Zeilen (Run-Meta), die der Parser nicht erkannt hat — gleicher
    Schutz wie bei @gc-session, sonst frisst ein Save das Meta still."""
    raw = len(GC_LAST_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("gc_last"))
    return max(0, raw - parsed)


def lost_id_lines(text: str, board: dict) -> int:
    """@gc-id:-Zeilen, die der Parser nicht als Item-ID erkannt hat (z.B. >1 ID je
    Item) → blockt Save, statt eine Run-Identität still zu verlieren."""
    raw = len(GC_ID_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("id"))
    return max(0, raw - parsed)


def lost_parent_lines(text: str, board: dict) -> int:
    """@gc-parent:-Zeilen, die der Parser nicht als Eltern-Zeiger erkannt hat (z.B. >1 je
    Item) → blockt Save, statt die Hierarchie-Kante still zu verlieren. Gleiche Disziplin
    wie bei @gc-id: die Kante IST hier das Feature."""
    raw = len(GC_PARENT_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("parent"))
    return max(0, raw - parsed)


def lost_wait_lines(text: str, board: dict) -> int:
    """@wait:-Zeilen, die der Parser nicht als Warte-Feld erkannt hat (z.B. >1 je Item
    oder außerhalb eines Items) → blockt Save, statt das Feld still zu verlieren."""
    raw = len(WAIT_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("wait") or it.get("wait_since"))
    return max(0, raw - parsed)


def lost_done_at_lines(text: str, board: dict) -> int:
    """@done-at:-Zeilen, die der Parser nicht als Stempel erkannt hat (z.B. >1 je
    Item) → blockt Save, statt das Feld still zu verlieren."""
    raw = len(DONE_AT_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("done_at"))
    return max(0, raw - parsed)


def lost_on_lines(text: str, board: dict) -> int:
    """@on:-Zeilen (Termin-To-do-Stichtag), die der Parser nicht erkannt hat (z.B. >1 je
    Item) → blockt Save, statt das Feld still zu verlieren. Analog zu lost_wait_lines."""
    raw = len(ON_LINE_RE.findall(guard_scope(text, board)))
    parsed = sum(1 for _s, _n, _c, it in _all_items(board) if it.get("on"))
    return max(0, raw - parsed)


def lost_stage_lines(text: str, board: dict) -> int:
    """@stage:-Zeilen MIT einem echten Stufenwert, die der Parser nicht als Stage-Event
    verbucht hat → blockt Save, statt eine Prozess-Stufe still zu verlieren. Zeilen OHNE
    Stufenwert (`@stage:` leer) zählen bewusst NICHT mit: die sind laut Failsafe-Regel gar
    kein Stage-Event, sondern werden als Body-Zeile durchgereicht (kein Verlust, kein
    False-Positive-Block). Zählt pro SPLIT-Zeile (wie parse_board), NICHT per Multiline-
    Regex auf dem Fließtext — sonst könnte `\\s?` bei einer leeren `@stage:`-Zeile den
    Zeilenumbruch fressen und die nächste Zeile fälschlich als Notiz einlesen."""
    body_text = guard_scope(text, board)
    raw = sum(1 for line in body_text.split("\n")
              if (m := STAGE_RE.match(line)) and _parse_stage(m.group(1)))
    parsed = sum(len(it.get("stages", [])) for _s, _n, _c, it in _all_items(board))
    return max(0, raw - parsed)


def lost_total(text: str, board: dict) -> int:
    """Summe aller lost-Guards — >0 heißt: ein serialize-Save würde Rohtext-Zeilen
    still vernichten. JEDER Schreibpfad muss das prüfen, nicht nur /api/board."""
    return (lost_boxes(text, board) + lost_thread_events(text, board)
            + lost_session_lines(text, board) + lost_id_lines(text, board)
            + lost_wait_lines(text, board) + lost_done_at_lines(text, board)
            + lost_gc_last_lines(text, board) + lost_on_lines(text, board)
            + lost_stage_lines(text, board) + lost_parent_lines(text, board)
            + lost_sessions_lines(text, board))


MAX_SESSIONS_HISTORY = 10  # Kappung der @gc-sessions:-Verlaufsliste (10.08.)


def _retire_session(it: dict, old_session: str, new_session: str) -> None:
    """Beim Wechsel des Resume-Pointers (@gc-session) die ABGELÖSTE UUID vorne in
    @gc-sessions: einreihen (10.08.: "Liste alter Session-UUIDs am Item mitführen" —
    nach einem Kontext-Schnitt/Neustart will er zur vorigen Session zurückblättern können,
    die @gc-session: sonst kommentarlos überschreibt; das volle Transkript liegt weiter
    unter ~/.claude/projects/<slug>/<uuid>.jsonl, nur der Board-Pointer verschwand).
    Bare UUID (kein Label — das steht nur am jeweils aktuellen Pointer), Duplikate raus,
    bei MAX_SESSIONS_HISTORY gekappt. No-op ohne alten Pointer oder wenn der neue
    dieselbe Session fortsetzt (--resume schreibt denselben Handle zurück, keine echte
    Ablösung — sonst würde jeder normale Reply-Write die eigene Session in die
    Verlaufsliste stopfen)."""
    old_uuid = gc_runner.session_uuid(old_session)
    new_uuid = gc_runner.session_uuid(new_session)
    if not old_uuid or old_uuid == new_uuid:
        return
    hist = [u for u in it.get("sessions", []) if u != old_uuid]
    it["sessions"] = ([old_uuid] + hist)[:MAX_SESSIONS_HISTORY]


def _new_id(existing: set[str]) -> str:
    """Kollisionssichere 12-hex-ID. Retry statt Hoffnung — eine doppelte
    Run-Identität würde find_item still aufs falsche Item lenken."""
    while True:
        nid = uuid.uuid4().hex[:12]
        if nid not in existing:
            return nid


def _orphan_ids_by_title(known: set[str], sidecar_dir: Path, days: int = 7) -> dict[str, str]:
    """Titel → verwaiste Item-ID: frische Faden-Dateien, deren ID es im Board nicht
    (mehr) gibt. Quelle für den Identitäts-Guard in `ensure_ids`.

    Die Lost-Guards zählen ZEILEN — sie merken nicht, wenn ein direkter Hand-Edit die
    `@gc-id:`-Zeile eines Items mitnimmt: die Zeilenbilanz bleibt sauber, `lost_total`
    ist 0, und `ensure_ids` stempelt eine frische ID. Faden, Subs und Sidecars hängen
    danach an einer Adresse, die kein Item mehr trägt (real passiert 2026-07-20 und
    2026-08-06; nachts meldet Guard 12 in `context-health-check.py` genau das).

    Der Titel im Sidecar-Header (`# <Label>: <Titel>`) ist der Diskriminator: nur wenn
    ein ID-loses Item exakt so heißt wie die verwaiste Faden-Datei, ist es dasselbe.
    Mehrdeutige Titel (zwei verschiedene verwaiste IDs) fallen deshalb raus.

    Warum ≤7 Tage: bewusst gelöschte Items lassen ihre Faden-Dateien liegen — ohne
    Fenster würde jede Altlast als Reklamations-Kandidat gelten (gemessen 06.08.:
    6 verwaiste IDs gesamt, 1 echter Verlust von heute). Gleiche Grenze wie Guard 12."""
    cutoff = time.time() - days * 86400
    found: dict[str, set[str]] = {}
    for f in sidecar_dir.glob("*.md"):
        gc_id = f.name[:12]
        if gc_id in known or not re.fullmatch(r"[0-9a-f]{12}", gc_id):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                continue
            with f.open(encoding="utf-8") as fh:
                head = fh.readline()
        except OSError:
            continue
        if head.startswith("# ") and ": " in head:
            found.setdefault(head.strip().split(": ", 1)[1], set()).add(gc_id)
    return {t: ids.pop() for t, ids in found.items() if len(ids) == 1}


def ensure_ids(board: dict, reclaim_dir: Path | None = None) -> None:
    """Jedem Item ohne stabile ID eine geben (lazy-Migration beim nächsten Save).
    Die ID ist die immutable Run-Identität — überlebt Umbenennen/Verschieben.

    Mit `reclaim_dir` (Sidecar-Verzeichnis) läuft zusätzlich der Identitäts-Guard:
    Bevor eine FRISCHE ID gewürfelt wird, bekommt ein Item seine alte zurück, wenn eine
    junge Faden-Datei mit exakt seinem Titel auf eine ID zeigt, die weder im Board noch
    im Archiv steht. Das Archiv zählt mit, weil abgehakte Items legitim aus board.md
    verschwinden — ihre IDs sind nicht verwaist, nur umgezogen.

    Der Guard kann einen Save nie scheitern lassen: er wählt bloß zwischen „alte ID
    zurück" und „neue ID würfeln" (= bisheriges Verhalten), und jeder Fehler beim
    Lesen der Sidecars fällt still auf das bisherige Verhalten zurück. Bewusst
    KEIN 409 — ein blockierender Guard hat am 30.07. schon einmal das ganze Board
    gesperrt; hier wäre der Preis eines Fehlalarms höher als der Schaden."""
    existing = {it["id"] for _s, _n, _c, it in _all_items(board) if it.get("id")}
    idless = [it for _s, _n, _c, it in _all_items(board) if not it.get("id")]
    orphans: dict[str, str] = {}
    if idless and reclaim_dir is not None:
        try:
            known = set(existing)
            if BOARD_ARCHIVE.exists():
                known |= set(re.findall(r"[0-9a-f]{12}", BOARD_ARCHIVE.read_text()))
            orphans = _orphan_ids_by_title(known, reclaim_dir)
        except Exception as e:  # Guard darf nie ein Save kippen
            print(f"todo-board: Identitäts-Guard übersprungen: {e}", file=sys.stderr)
    for it in idless:
        claimed = orphans.pop((it.get("title") or "").strip(), "")
        if claimed and claimed not in existing:
            it["id"] = claimed
            print(f"todo-board: Identitäts-Guard — Item „{it.get('title')}“ bekommt seine "
                  f"verwaiste ID {claimed} zurück statt einer neuen")
        else:
            it["id"] = _new_id(existing)
            # Sichtbar machen, was der Guard NICHT retten konnte. Aus dem Text
            # allein ist „hatte nie eine ID" nicht von „hat gerade seine verloren"
            # zu unterscheiden — wer den Faden vermisst, findet hier den Hinweis,
            # wo die verwaiste Sidecar-Datei liegt.
            print(f"todo-board: Item „{it.get('title')}“ bekommt eine NEUE @gc-id "
                  f"({it['id']}); falls es vorher einen Faden hatte, liegt der jetzt "
                  f"verwaist in {reclaim_dir or sidecar.SIDECAR_DIR}", file=sys.stderr)
        existing.add(it["id"])


def thread_status(it: dict) -> str:
    """Wer ist dran? Stateless aus dem letzten Faden-Turn abgeleitet.
    none = kein Faden · for_gc = wartet auf den Agenten (letzter Turn @gc:)
    · for_owner = GC hat geantwortet · closed = @gc-done.
    System-Turns (@gc-sys:, Sub-Roll-up) zählen NICHT: sie sind Beiwerk, kein Zug einer
    der beiden Seiten. Ohne diese Filterung machte ein automatischer Roll-up aus einem
    „wartet auf GC" ein „du bist dran" (Sol-Befund 1)."""
    t = [e for e in it.get("thread", []) if e["kind"] != "sys"]
    if not t:
        return "none"
    return {"done": "closed", "reply": "for_owner", "ask": "for_gc"}[t[-1]["kind"]]


# ---------------------------------------------------------------- Hierarchie
# Flache Items + @gc-parent-Zeiger (Design abgenommen 2026-07-23). Alles hier ist reine
# LESE-Auflösung über dem flachen Board — kein Baum, kein Zustand, keine Migration.
#
# Tiefen-/Zyklen-Guard (Sol-Auflage): eine Kante gilt NUR, wenn das Eltern-Item selbst
# KEINEN @gc-parent trägt. Das erzwingt genau eine Ebene (Subs haben keine Subs) und macht
# Zyklen strukturell unmöglich — bei A→B→A trägt jeder von beiden einen Zeiger, also ist
# keine der Kanten gültig. Eine ungültige Kante wird NIE aus der Datei entfernt (Failsafe:
# nichts still löschen), sie ist nur wirkungslos.
ROLLUP_TTL_MIN = 120  # Anzeige-Lebensdauer der ✓-Roll-up-Zeile (Owner: „nach ~2h weg")
ROLLUP_MARK_RE = re.compile(r"\[sub:([0-9a-zA-Z]{4,32})\]")


def item_index(board: dict) -> dict[str, dict]:
    """id → Item über das ganze Board (erste Bindung gewinnt; _new_id hält IDs eindeutig)."""
    idx: dict[str, dict] = {}
    for _s, _n, _c, it in _all_items(board):
        if (i := it.get("id")) and i not in idx:
            idx[i] = it
    return idx


def parent_of(it: dict, idx: dict[str, dict]) -> dict | None:
    """Gültiges Eltern-Item oder None (unbekannte ID, Selbstreferenz, Eltern ist selbst
    ein Sub → Kante wirkungslos)."""
    pid = (it.get("parent") or "").strip()
    if not pid or pid == it.get("id"):
        return None
    par = idx.get(pid)
    if par is None or par.get("parent"):
        return None
    return par


def children_of(board: dict, parent_id: str, idx: dict[str, dict] | None = None) -> list[dict]:
    """Alle Items mit gültiger Kante auf dieses Eltern-Item, in Board-Reihenfolge."""
    if not parent_id:
        return []
    idx = idx if idx is not None else item_index(board)
    return [it for _s, _n, _c, it in _all_items(board)
            if (p := parent_of(it, idx)) is not None and p.get("id") == parent_id]


def _child_result(child: dict) -> str:
    """Ergebnistext eines erledigten Subs = seine LETZTE echte Antwort. Gibt es keine
    (von Hand abgehakt, oder letzter Turn ist eine Rückfrage), sagen wir das ehrlich —
    Status und Ergebnis sind getrennt, und eine erfundene Zusammenfassung wäre schlimmer
    als keine (Sol-Befund 3)."""
    replies = [e.get("text", "") for e in child.get("thread", []) if e["kind"] == "reply"]
    if not replies:
        return "(no result text — checked off manually)"
    txt = replies[-1].split(" · ")[0].strip()
    return (txt[:137] + "…") if len(txt) > 138 else (txt or "(empty reply)")


def rollup_child_completions(board: dict, now: datetime | None = None) -> int:
    """Erledigte Sub-Fäden als System-Turn ins Elternitem hochrollen. EIN idempotenter
    Handler (keyed by Child-ID über den `[sub:<id>]`-Marker) — egal über welchen
    Schreibpfad das Sub erledigt wurde (Klick, Agent, Hand-Edit). Gibt die Anzahl neu
    geschriebener Zeilen zurück (0 = nichts zu tun, board unverändert).

    Bewusst KEIN Auto-Abhaken des Elternitems (Owner: Nudge, kein Automatismus) — der
    Fortschritt wird überall live aus dem Ist-Zustand gerendert, nie aus dieser Zeile.
    Ein wieder aufgemachtes Sub bekommt also automatisch wieder ⏳, ohne dass die alte
    Roll-up-Zeile gelöscht werden müsste (append-only bleibt append-only)."""
    now = now or datetime.now()
    idx = item_index(board)
    written = 0
    for _s, _n, _c, it in _all_items(board):
        if not it.get("done"):
            continue
        par = parent_of(it, idx)
        if par is None:
            continue
        cid = it.get("id", "")
        seen = {m.group(1) for e in par.get("thread", []) if e["kind"] == "sys"
                for m in ROLLUP_MARK_RE.finditer(e.get("text", ""))}
        if cid in seen:
            continue
        stamp = now.strftime("%Y-%m-%d %H:%M")
        par.setdefault("thread", []).append(
            {"kind": "sys", "text": f"✓ Sub erledigt: {it.get('title', '')} [sub:{cid}] · "
                                    f"{_child_result(it)} *({stamp})*"})
        written += 1
    return written


# Modell-Aliasse für Agent-Runs. Jeder nicht-leere Wert wird als `--model <alias>` an
# claude gereicht und ERZWINGT so das Modell — auch beim Resume, wo sonst das klebrige
# Session-Modell gewinnt (2026-07-15: Fable-Auswahl muss Fable enforcen, nicht nur
# "kein Flag" heißen). "" = kein Flag = CLI-Default nur bei frischer Session; bleibt für
# Alt-localStorage in der Whitelist, die UI bietet es aber nicht mehr an.
# Whitelist statt Freitext: ein Tippfehler im Slug scheiterte erst nach Minuten sichtbar.
# Seit 2026-07-27 sind die Werte Lauf-PROFILE (Modell + Effort), nicht mehr nur Modelle —
# die Zuordnung steht als einzige Quelle in gc_runner.RUN_PROFILES, hier hängt nur die
# Eingangsprüfung dran. Wire-Feldname bleibt `model` (Alt-Clients, Alt-localStorage).
import gc_runner  # noqa: E402 — sonst überall lazy importiert, hier zwingend beim Laden,
MODEL_CHOICES = tuple(gc_runner.RUN_PROFILES)  # weil die Whitelist eine Modul-Konstante ist.
# Zyklusfrei: der Runner importiert den Server nicht, sein Import hat keine Nebenwirkungen.


STREAM_TAIL_BYTES = 3_000_000  # nur den Schwanz lesen — ein langer Run schreibt viele MB
STREAM_MAX_ROWS = 250          # so viele Schritte zeigt das Overlay (die jüngsten)
STREAM_PREVIEW = 220           # Zeichen pro Vorschau; Werkzeug-Ergebnisse sind sonst endlos
# ── SSE (Phase 3, 2026-08-10: neuer Endpoint NEBEN /api/gc-stream, alt bleibt) ──
SSE_POLL_S = 1.0               # Tail-Takt der offenen Verbindung; Deltas kommen quasi sofort
SSE_HEARTBEAT_S = 15           # Kommentar-Ping — ein toter Socket (Tab zu, Mac im Sleep)
                               # fällt spätestens beim nächsten Schreibversuch auf
SSE_MAX_CONN_S = 2 * 3600      # Sicherheitsnetz: keine Verbindung lebt ewig. Reißt die Kappe,
                               # schließen wir OHNE end-Event — EventSource verbindet von selbst
                               # neu und liest per Last-Event-ID nahtlos weiter


def _preview(value) -> str:
    """Beliebiger Ereignis-Inhalt → kurze einzeilige Vorschau."""
    if isinstance(value, list):
        value = " ".join(_preview(v) for v in value)
    elif isinstance(value, dict):
        value = value.get("text") or value.get("content") or json.dumps(value, ensure_ascii=False)
        if not isinstance(value, str):
            value = _preview(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:STREAM_PREVIEW] + ("…" if len(text) > STREAM_PREVIEW else "")


def _codex_tool_row(item: dict, started: bool) -> dict | None:
    """Ein Werkzeug-Item des Codex-Stroms → Aufruf- bzw. Ergebniszeile.

    Der Werkzeugname steckt bei Codex nicht in einem Feld, sondern IM ITEM-TYP —
    dieselbe Logik wie in `gc_runner.CodexStreamTail`, nur fürs Vorlesen statt fürs
    Zählen. Bei `command_execution` steht der Befehl im Namen (sonst hieße jede zweite
    Zeile „command_execution"), bei `mcp_tool_call` Server/Werkzeug."""
    itype = str(item.get("type") or "")
    name, arg = itype, ""
    if itype == "command_execution":
        cmd = str(item.get("command") or "").strip()
        # Codex verpackt jeden Aufruf in `/bin/zsh -lc "…"`. Stünde das im Namen, hieße
        # jede Zeile gleich — also Hülle abziehen und das echte Kommando zeigen.
        inner = re.sub(r'^\S*(?:sh|zsh|bash)\s+-[a-z]*c\s+', "", cmd).strip("'\" \n")
        first = (inner or cmd).split()[0] if (inner or cmd).split() else itype
        name = f"shell: {first.split('/')[-1][:24]}"
        arg = inner or cmd
    elif itype == "mcp_tool_call":
        name = f"{item.get('server', '?')}/{item.get('tool', '?')}"
        arg = _preview(item.get("arguments") or item.get("input") or "")
    elif itype == "file_change":
        changes = [c for c in (item.get("changes") or []) if isinstance(c, dict)]
        arg = ", ".join(f"{c.get('kind', '?')} {str(c.get('path', '')).split('/')[-1]}"
                        for c in changes)
        name = f"file_change ({len(changes)})" if changes else itype
    elif itype == "web_search":
        arg = _preview(item.get("query") or "")
    if started:
        return {"kind": "tool", "tool": name, "text": _preview(arg)}
    # Abgeschlossen: Ergebniszeile. Fehler erkennen wir an Status ODER Exit-Code —
    # Codex setzt je nach Item-Typ nur eines von beidem.
    status = str(item.get("status") or "")
    try:
        exit_code = int(item.get("exit_code")) if item.get("exit_code") is not None else 0
    except (TypeError, ValueError):
        exit_code = 0
    out = item.get("aggregated_output") or item.get("output") or item.get("result") or ""
    return {"kind": "result", "error": exit_code != 0 or status in ("failed", "error"),
            "text": _preview(out or arg or status or itype)}


def _codex_row(ev: dict) -> dict | None:
    """Ein Ereignis des Codex-Stroms (`codex exec --json`) → dieselbe Zeilenform wie bei
    claude (2026-08-12, Blatt Runde 3 Q2 = „gleiche Optik wie Claude").

    Warum das hier neben den claude-Zweigen steht und nicht als zweiter Zeilen-Bauer mit
    eigener Runner-Erkennung: die Typ-Namen beider Runner sind DISJUNKT
    (`thread.started`/`item.*`/`turn.*` gegen `system`/`assistant`/`user`/`result`), das
    Ereignis erkennt sich also selbst. Damit brauchen weder Polling- noch SSE-Weg zu
    wissen, wer läuft — und ein Journal mit beidem drin (Runner-Wechsel am selben Item)
    kann gar nicht falsch gelesen werden."""
    kind = ev.get("type")
    if kind == "thread.started":
        return {"kind": "start", "text": "Started · Codex"}
    if kind in ("item.started", "item.completed"):
        item = ev.get("item")
        if not isinstance(item, dict):
            return None
        itype = str(item.get("type") or "")
        if itype == "agent_message":
            text = str(item.get("text") or "").strip()
            # Nur beim Abschluss zeigen, sonst stünde jede Antwort doppelt im Strom.
            return {"kind": "say", "text": _preview(text)} if (
                kind == "item.completed" and text) else None
        if itype == "todo_list":
            items = [i for i in (item.get("items") or []) if isinstance(i, dict)]
            done = sum(1 for i in items if i.get("completed"))
            return {"kind": "tool", "tool": f"Plan ({done}/{len(items)})",
                    "text": _preview(" · ".join(str(i.get("text", "")) for i in items))
                    } if kind == "item.started" and items else None
        if itype in gc_runner.CODEX_TOOL_ITEMS:
            return _codex_tool_row(item, kind == "item.started")
        return None  # reasoning & Co: Rauschen, wie die Hook-Ereignisse bei claude
    if kind == "turn.completed":
        usage = ev.get("usage") or {}
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        detail = f" · {inp} in / {out} out" if inp or out else ""
        return {"kind": "done", "text": f"Done (Codex){detail}"}
    if kind in ("turn.failed", "error", "thread.error"):
        msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else None
        return {"kind": "result", "error": True,
                "text": _preview(msg or ev.get("message") or kind)}
    return None  # turn.started und alles Unbekannte: still


def _opencode_row(ev: dict) -> dict | None:
    """An OpenCode JSONL event → the overlay's shared row shape.

    OpenCode reports tool calls only after they finish. So per `tool_use` we show
    exactly one honest tool line with input (or, if that is missing, output),
    instead of inventing a started and a finished call. `step_start` must be
    visible: it is the first event and immediately replaces the "loading…"
    placeholder in the browser.
    """
    kind = ev.get("type")
    part = ev.get("part")
    part = part if isinstance(part, dict) else {}
    if kind == "step_start":
        return {"kind": "start", "text": "Started · OpenCode"}
    if kind == "tool_use":
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        status = str(state.get("status") or "")
        detail = state.get("input") or state.get("output") or state.get("error") or status
        return {"kind": "tool", "tool": str(part.get("tool") or "tool"),
                "error": status in ("error", "denied"), "text": _preview(detail)}
    if kind == "text":
        text = str(part.get("text") or "").strip()
        return {"kind": "say", "text": _preview(text)} if text else None
    if kind == "step_finish":
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        total = tokens.get("total")
        detail = f" · {total} tokens" if total is not None else ""
        return {"kind": "result", "text": f"Step finished{detail}"}
    return None


def _stream_row(ev: dict) -> dict | None:
    """Ein Roh-Ereignis → eine Zeile fürs Overlay. None = für Menschen uninteressant
    (Hook-Geräusch, Thinking-Token-Zähler)."""
    kind = ev.get("type")
    if isinstance(kind, str) and (kind.startswith(("item.", "turn.", "thread.")) or kind == "error"):
        return _codex_row(ev)
    if kind in ("step_start", "tool_use", "step_finish") or (
            kind == "text" and isinstance(ev.get("part"), dict)):
        return _opencode_row(ev)
    if kind == "system":
        if ev.get("subtype") != "init":
            return None  # hook_started/hook_response/thinking_tokens: reines Rauschen
        return {"kind": "start", "text": f"Started · model {ev.get('model', '?')}"}
    if kind == "rate_limit_event":
        status = str(ev.get("rate_limit_info", {}).get("status", ""))
        return None if status in ("", "allowed") else {"kind": "rate", "text": f"Rate-Limit: {status}"}
    if kind == "assistant":
        rows = []
        for b in ev.get("message", {}).get("content", []) or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                rows.append({"kind": "tool", "tool": str(b.get("name", "")),
                             "text": _preview(b.get("input", ""))})
            elif b.get("type") == "text" and str(b.get("text", "")).strip():
                rows.append({"kind": "say", "text": _preview(b.get("text"))})
        return rows[0] if len(rows) == 1 else ({"kind": "multi", "rows": rows} if rows else None)
    if kind == "user":
        for b in ev.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return {"kind": "result", "error": bool(b.get("is_error")),
                        "text": _preview(b.get("content", ""))}
        return None
    if kind == "result":
        return {"kind": "done", "text": f"Done ({ev.get('subtype')}) · {ev.get('num_turns')} turns"}
    return None


def stream_view(journal_dir: Path, gc_id: str, live: bool) -> dict:
    """Ereignisstrom eines Runs als lesbare Schrittliste.

    Quelle: der laufende Run (journal/run-<id>-*.out.json) — sonst der aufgehobene Strom
    des zuletzt gekillten Runs (journal/killed/). Erfolgreiche Runs räumen ihr Journal ab,
    deren Strom ist also bewusst weg; dafür steht die Antwort ja im Faden.

    Gelesen wird nur der SCHWANZ der Datei: ein 30-min-Run schreibt zweistellige MB,
    und interessant ist ohnehin „wo steht er gerade"."""
    # mtime defensiv holen: zwischen glob() und stat() darf die Journal-Wache die Datei
    # wegräumen — das wäre sonst ein 500 statt einer leeren Liste.
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    src, note = None, ""
    hits = sorted(journal_dir.glob(f"run-{gc_id}-*.out.json"), key=_mtime)
    if hits:
        src, note = hits[-1], ("running now" if live else "latest run (not cleared yet)")
    elif live:
        # The registry is set before the child creates its journal. Never surface an
        # older killed stream during that startup gap — it describes another run and
        # made a fresh launch look stopped before it had even begun.
        return {"rows": [], "note": "running now", "truncated": False, "total": 0,
                "size_mb": 0.0, "empty": "", "profile": "", "waiting": True}
    else:
        killed = sorted((journal_dir / "killed").glob(f"run-{gc_id}-*.jsonl"),
                        key=_mtime) if (journal_dir / "killed").is_dir() else []
        if killed:
            src, note = killed[-1], f"aborted run ({killed[-1].name.split('.')[-2]})"
    if src is None:
        return {"rows": [], "note": "", "empty": "No event stream is available — the run either succeeded "
                                              "(journal cleared) or predates this feature"}
    try:
        size = src.stat().st_size
        with open(src, errors="replace") as f:
            if size > STREAM_TAIL_BYTES:
                f.seek(size - STREAM_TAIL_BYTES)
                f.readline()  # angeschnittene erste Zeile wegwerfen
            raw = f.read()
    except OSError as e:
        return {"rows": [], "note": "", "empty": f"Cannot read event stream: {e}"}
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # halbe Schlusszeile eines gekillten Runs
        if not isinstance(ev, dict):
            continue
        row = _stream_row(ev)
        if row is None:
            continue
        rows.extend(row["rows"]) if row.get("kind") == "multi" else rows.append(row)
    truncated = len(rows) > STREAM_MAX_ROWS
    for i, r in enumerate(rows[-STREAM_MAX_ROWS:], start=max(0, len(rows) - STREAM_MAX_ROWS) + 1):
        r["n"] = i
    meta_path = src.with_name(src.name.removesuffix(".out.json") + ".meta.json")
    try:
        profile = str(json.loads(meta_path.read_text()).get("model") or "")
    except (OSError, ValueError, TypeError):
        profile = ""
    return {"rows": rows[-STREAM_MAX_ROWS:], "note": note, "truncated": truncated,
            "total": len(rows), "size_mb": round(size / 1e6, 1), "empty": "",
            "profile": profile, "waiting": bool(live and not rows)}


_KILL_CACHE: dict = {"mtime": -1.0, "day": "", "rows": []}


def killed_today(journal_dir: Path | None = None) -> list[dict]:
    """Heute abgebrochene Runs (aus journal/killed-runs.jsonl) — Datengrundlage für die
    leise Notiz oben im Board (2026-07-27, Blatt Q5: „oben im board eine notification,
    was halt leicht ist, lass uns simple bleiben"). Kein Push-Kanal, keine Desktop-Meldung:
    sichtbar, sobald er hinschaut, und sonst still.

    Gecacht über mtime UND Datum — die Liste wird bei jedem /api/etag (alle 5 s) abgefragt.
    Das Datum gehört zwingend in den Schlüssel: die mtime allein hätte das Ergebnis von
    gestern über Mitternacht hinweg festgehalten, solange niemand einen neuen Run killt.
    Der Server läuft als Dauerprozess, also blieb „⚠ N Runs heute abgebrochen" am nächsten
    Morgen stehen — mit den Abbrüchen von gestern (2026-07-29: „Seems outdated")."""
    path = (journal_dir / "killed-runs.jsonl") if journal_dir else _gc_runner_kill_log()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    today = time.strftime("%Y-%m-%d")
    if _KILL_CACHE["mtime"] != mtime or _KILL_CACHE.get("day") != today:
        rows = []
        try:
            for line in path.read_text().splitlines()[-200:]:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if str(row.get("ts", "")).startswith(today):
                    rows.append({k: row.get(k) for k in
                                 ("ts", "gc_id", "title", "reason", "elapsed_min", "last_tool")})
        except OSError:
            rows = []
        _KILL_CACHE.update(mtime=mtime, day=today, rows=rows)
    return _KILL_CACHE["rows"]


def _gc_runner_kill_log() -> Path:
    import gc_runner  # lazy — Stil wie an den anderen Aufrufstellen
    return gc_runner.KILL_LOG


# ── Verbindungs-Check ────────────────────────────────────────────────────────
# Diagnose für den Fall „ein Run läuft ewig" (2026-08-06, Blatt „Slow connection
# marker"): die Pill färbt sich ab 20 min gelb, sagt aber nichts darüber, WARUM. Zwei
# ganz verschiedene Ursachen sehen für den Owner identisch aus — schlechte Leitung am Mac
# (dann bringt Warten nichts) oder ein zäher/hängender Run. Der Check misst deshalb
# beides getrennt: eine unabhängige, schnelle Adresse (ist das Internet überhaupt da?)
# und den Anthropic-Status (Q3 seine Ergänzung: „dann sehen wir auch, wenn das Modell
# gerade langsam ist, während Internet noch läuft").
#
# Bewusst GETRIGGERT, nicht periodisch (Blatt Q1=A): der Check läuft nur, wenn die UI
# ihn anfragt, und das tut sie nur, solange ein Run die 20-min-Schwelle reißt. Kein
# Dauer-Traffic aus einem Board, das nur offen im Tab liegt.
NETCHECK_TTL = 60.0          # Ergebnis so lange wiederverwenden (die UI fragt öfter)
NETCHECK_SLOW_MS = 1200      # ab hier „langsam"
NETCHECK_DEAD_MS = 4000      # ab hier „praktisch tot"
NETCHECK_TIMEOUT = 5.0
# Reichweiten-Messung bewusst als roher TCP-Handshake gegen eine IP (Cloudflare): kein DNS,
# kein TLS, keine Zertifikatskette — nur „komme ich raus und wie schnell". Gemessen
# 2026-08-06: das systemeigene python3 auf dem Mac kann HTTPS ohne certifi gar nicht
# verifizieren (CERTIFICATE_VERIFY_FAILED), ein HTTPS-Ping hätte also dauerhaft
# „kein Internet" gemeldet, während curl daneben tadellos lief.
NETCHECK_PING_HOST, NETCHECK_PING_PORT = "1.1.1.1", 443
# status.anthropic.com leitet auf status.claude.com um (geprüft 2026-08-06) — direkt die
# Zieladresse nehmen, spart den Redirect-Hop bei genau der Messung, die Latenz misst.
NETCHECK_STATUS_URL = "https://status.claude.com/api/v2/status.json"
_NET_CACHE: dict = {"ts": 0.0, "data": None}
_NET_LOCK = threading.Lock()


def _tcp_probe(host: str, port: int) -> dict:
    """Reine Erreichbarkeit: Dauer des TCP-Handshakes in ms."""
    import socket

    t0 = time.monotonic()
    try:
        socket.create_connection((host, port), timeout=NETCHECK_TIMEOUT).close()
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000)}
    except OSError as exc:
        return {"ok": False, "ms": round((time.monotonic() - t0) * 1000),
                "error": type(exc).__name__}


def _http_probe(url: str) -> dict:
    """HTTP-Messung: Dauer in ms + Rohtext (gekappt). Fehler ist ein Ergebnis, kein
    Abbruch. `cert` unterscheidet „kaputte Zertifikatskette im Python" von „Netz weg" —
    ohne certifi darf das keine Anthropic-Störung vortäuschen."""
    import ssl
    import urllib.request

    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gc-board-netcheck"})
        with urllib.request.urlopen(req, timeout=NETCHECK_TIMEOUT, context=ctx) as r:
            body = r.read(4096).decode("utf-8", "replace")
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "body": body}
    except Exception as exc:  # noqa: BLE001 — DNS, TLS, Timeout, HTTP: alles ein Befund
        return {"ok": False, "ms": round((time.monotonic() - t0) * 1000),
                "error": type(exc).__name__,
                "cert": isinstance(getattr(exc, "reason", None), ssl.SSLError)}


def netcheck(force: bool = False) -> dict:
    """{level, msg, ping_ms, api_ms, indicator, checked} — level "" heißt: alles gut,
    die UI zeigt dann nichts. Ergebnis 60 s gecacht, parallel gemessen."""
    with _NET_LOCK:
        cached = _NET_CACHE["data"]
        if cached and not force and time.time() - _NET_CACHE["ts"] < NETCHECK_TTL:
            return cached
    out: dict[str, dict] = {}
    jobs = (("ping", lambda: _tcp_probe(NETCHECK_PING_HOST, NETCHECK_PING_PORT)),
            ("api", lambda: _http_probe(NETCHECK_STATUS_URL)))
    threads = [threading.Thread(target=lambda k=k, f=f: out.__setitem__(k, f()), daemon=True)
               for k, f in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(NETCHECK_TIMEOUT + 1)
    ping = out.get("ping", {"ok": False, "ms": 0, "error": "NoResult"})
    api = out.get("api", {"ok": False, "ms": 0, "error": "NoResult"})

    indicator, desc = "", ""
    if api.get("ok"):
        try:
            js = json.loads(api["body"])
            indicator = str(js.get("status", {}).get("indicator", ""))
            desc = str(js.get("status", {}).get("description", ""))
        except (json.JSONDecodeError, ValueError, AttributeError):
            indicator = ""

    level, msgs = "", []
    if not ping["ok"]:
        level, msgs = "bad", ["This Mac is offline (ping did not respond)"]
    elif ping["ms"] >= NETCHECK_DEAD_MS:
        level, msgs = "bad", [f"Connection is very slow ({ping['ms']} ms ping)"]
    elif ping["ms"] >= NETCHECK_SLOW_MS:
        level, msgs = "warn", [f"Connection is slow ({ping['ms']} ms ping)"]
    # Anthropic getrennt bewerten: Leitung kann tadellos sein und das Modell trotzdem zäh.
    if indicator in ("major", "critical"):
        level = "bad"
        msgs.append(f"Anthropic incident: {desc or indicator}")
    elif indicator == "minor":
        level = level or "warn"
        msgs.append(f"Anthropic reports: {desc or indicator}")
    elif not api["ok"] and ping["ok"] and not api.get("cert"):
        # Netz da, aber Anthropics Statusseite nicht erreichbar — schwaches Signal, aber
        # genau die Konstellation „nur der eine Weg klemmt". Zertifikatsfehler zählen
        # NICHT: das ist ein Python-Problem auf diesem Mac, keine Störung draußen.
        level = level or "warn"
        msgs.append("Anthropic status is unavailable")

    data = {"level": level, "msg": " · ".join(msgs), "ping_ms": ping["ms"],
            "ping_ok": ping["ok"], "api_ms": api["ms"], "indicator": indicator,
            "checked": time.time()}
    with _NET_LOCK:
        _NET_CACHE.update(ts=time.time(), data=data)
    return data


def _public_beats() -> dict:
    """BEATS für die UI — ohne die internen Pfade. Der Browser hat mit Journal- und
    Stopp-Pfaden nichts zu tun; die bleiben serverseitig (der Stopp läuft über die
    gc-id, nicht über einen vom Client geschickten Pfad — sonst wäre das ein
    hübscher Weg, den Server beliebige Dateien anlegen zu lassen).
    Aufrufer hält RUN_LOCK."""
    return {k: {kk: vv for kk, vv in v.items() if kk not in ("stop_path", "journal")}
            for k, v in BEATS.items()}


def request_stop(gc_id: str) -> str:
    """Stopp-Wunsch für einen laufenden Run hinterlegen. Gibt "" zurück, wenn es
    geklappt hat, sonst den Grund. Der eigentliche Kill passiert in gc_runner.watch_run
    — die kennt dann den Grund und schreibt „⏹ von dir gestoppt" statt „Absturz"."""
    with RUN_LOCK:
        beat = BEATS.get(gc_id)
        if gc_id not in RUNNING:
            return "The run is not active (it may have just finished)"
        if gc_id in COMPACTING:
            # Kompaktierungen laufen über einen anderen Pfad ohne Journal — sie sind
            # schlicht nicht stoppbar. Das ehrlich sagen statt „gleich nochmal"
            # zu versprechen, was nie eintritt. (Review-Fund F4)
            return "Context compaction cannot be stopped — it is almost done"
        stop_path = (beat or {}).get("stop_path") or ""
    if not stop_path:
        return "The run is just starting — wait a few seconds and try again"
    try:
        Path(stop_path).write_text(f"stop {time.time()}")
    except OSError as e:
        return f"Cannot write stop marker: {e}"
    return ""


# Der Neustart selbst wird hier NICHT ausgeloest — ein installiertes Paket startet
# ueber seinen eigenen Prozess neu, nicht ueber ein Skript im Repo. Der Waechter
# bleibt trotzdem: laeuft ein Neustart von aussen, darf kein neuer Run mehr starten.
RESTART_LOCK = Path("/tmp/board-restart.lock")
RESTART_DRAIN_MAX = 45 * 60   # s — länger als das MAX_WAIT des Wächters ⇒ Lock ist verwaist
RESTART_DRAIN_MSG = "Board restart in progress — the item exists; press ▶ again after the restart"


def restart_draining() -> bool:
    """True, solange ein Board-Neustart darauf wartet, dass die laufenden GC-Runs
    auslaufen. In diesem Fenster darf KEIN neuer Run mehr starten.

    Warum: restart-server.sh wartet, bis kein Agenten-Kind mehr läuft (ein Neustart
    mittendrin killt den Run und seine Antwort). Ohne Drain rücken auf einem normalen
    Board-Tag laufend neue Runs nach — der Wächter verhungert und läuft in sein MAX_WAIT.

    Ein verwaistes Lock (Wächter gestorben, rmdir nie gelaufen) blockiert nicht ewig:
    älter als RESTART_DRAIN_MAX wird ignoriert."""
    try:
        age = time.time() - RESTART_LOCK.stat().st_mtime
    except OSError:
        return False
    return age < RESTART_DRAIN_MAX


def launch_gc_run(pending: dict, base_url: str, claude_cmd: str, timeout: int,
                  semaphore: threading.Semaphore | None = None, model: str = "",
                  sidecar_dir: Path | None = None) -> bool:
    """Registriert das Item in RUNNING und startet den Agenten-Run als Daemon-Thread.
    False = lief schon ODER ein Neustart drainet gerade. Gemeinsamer Pfad für Einzel-Run
    und Run-all; das optionale Semaphor (Run-all-Parallellimit) wird IMMER released —
    auch bei Crash."""
    import gc_runner
    gc_id = pending["addr"]["id"]
    if restart_draining():
        if semaphore:
            semaphore.release()
        return False
    with RUN_LOCK:
        if gc_id in RUNNING:
            if semaphore:
                semaphore.release()
            return False
        RUNNING[gc_id] = time.time()
        BEATS[gc_id] = {"steps": 0, "last_tool": "", "session_id": "", "rate_limit": "",
                        "last_event": time.time(), "stop_path": ""}

    # Der Server darf gegen ein per --file umgelenktes Board laufen (Tests, Wegwerf-
    # Instanzen, spaeter das OSS-Package). Antworten muessen dann neben DIESEM Board
    # landen, nicht im beim Modulimport eingefrorenen produktiven _p.THREADS. Genau die
    # Asymmetrie leckte test_interrupt_und_weiter bei einem roten Lauf nach inbox/.
    sidecar_dir = sidecar_dir or getattr(Handler, "board_path", DEFAULT_BOARD).parent / "gc-threads"

    def beat(state: dict) -> None:
        """Lebenszeichen aus dem laufenden Agenten → Registry → Board-Anzeige.
        Merge statt Ersetzen: der allererste Ruf trägt nur den Journal-/Stopp-Pfad,
        spätere nur den Fortschritt — beides muss stehen bleiben."""
        with RUN_LOCK:
            if gc_id in BEATS:
                BEATS[gc_id].update(state)

    def work() -> None:
        try:
            gc_runner.run_item(pending, base_url, claude_cmd=claude_cmd, timeout=timeout,
                               sidecar_dir=sidecar_dir, model=model, on_beat=beat)
        except Exception as e:  # noqa: BLE001 — fail gracefully: Crash wird sichtbar, nie stumm
            try:
                # Stempel MIT posten: sonst steht der Crash nur im Faden und die Karte in
                # der Matrix sieht aus wie nie gelaufen (23.07.: „im sticker sehe ich
                # aber noch nicht den neuen incl. fail").
                gc_runner._post_append(base_url, gc_id, f"❌ Runner-Crash: {e}", "",
                                       gc_runner.fail_stamp())
            except Exception:
                print(f"gc-run: Crash für {gc_id} UND post-back tot: {e}")
        finally:
            with RUN_LOCK:
                RUNNING.pop(gc_id, None)
                BEATS.pop(gc_id, None)
            if semaphore:
                semaphore.release()
            try:
                _maybe_retrigger(pending, base_url, claude_cmd, timeout, model, sidecar_dir)
            except Exception as e:  # noqa: BLE001 — Retrigger-Fehler darf den Run nicht mitreißen
                print(f"todo-board: Auto-Retrigger für {gc_id} fehlgeschlagen: {e}")

    threading.Thread(target=work, daemon=True).start()
    return True


def _maybe_retrigger(pending: dict, base_url: str, claude_cmd: str, timeout: int, model: str,
                     sidecar_dir: Path | None = None) -> None:
    """Follow-up während des Laufs (2026-07-15): eine `@gc:`-Nachricht, die reinkam,
    NACHDEM dieser Run seinen Prompt schon gebaut hatte, wird von ihm nie gesehen — und
    landet trotzdem VOR der eigenen Antwort im Faden (Appends sind streng chronologisch).
    thread_status kippt dadurch auf "for_owner" (letzter Turn = unsere Antwort), obwohl die
    neue Nachricht nie bearbeitet wurde — sie bliebe sonst unbemerkt liegen.
    Fix: frisch lesen, alle `ask`-Turns zwischen Snapshot und eigener Antwort einsammeln,
    und wenn welche da sind, automatisch einen Folge-Run starten (Inline-Hinweis im Prompt
    statt separatem Faden-Element — Owner-Entscheidung 2026-07-15, Option A)."""
    gc_id = pending["addr"]["id"]
    snapshot_len = len(pending.get("thread", []))
    raw = Handler.board_path.read_text()
    board = parse_board(raw)
    hit = next(((s, n, c, it) for s, n, c, it in _all_items(board) if it.get("id") == gc_id), None)
    if hit is None:
        return
    s, n, c, it = hit
    thread = it.get("thread", [])
    # thread[-1] ist die gerade gepostete eigene Antwort (Erfolg oder ❌-Crash) — alles
    # dazwischen kam während der Laufzeit rein. Nur "ask": ein "done" (Faden geschlossen,
    # während der Run lief) heißt bewusst NICHT weitermachen.
    missed = [e["text"] for e in thread[snapshot_len:-1] if e["kind"] == "ask"]
    if not missed:
        return
    new_pending = pending_entry(s, n, c, it, board)
    new_pending["last_ask"] = " · ".join(missed)
    # Zwei Anlässe, eine Mechanik — aber der Agent soll den Unterschied kennen: „Unterbrechen
    # & weiter" (2026-07-28) stoppt den Run ABSICHTLICH, weil die Nachricht die laufende
    # Arbeit ändern soll. Der ⏹-Turn ist unser Erkennungszeichen (fail_stamp/kill-Wortlaut).
    interrupted = (thread[-1].get("text") or "").lstrip().startswith("⏹")
    new_pending["retrigger_note"] = (
        "Note: you were deliberately stopped MID-WORK because this message arrived — it takes "
        "priority over what you were about to do. Check what you already started or wrote "
        "(partially finished changes are possible), then continue from there with the new information."
        if interrupted else
        "Note: this message arrived before the previous reply was finished — read the previous "
        "@gc-re: turn too if necessary.")
    print(f"todo-board: auto-retrigger for {gc_id} ({len(missed)} message(s) arrived during the run)")
    launch_gc_run(new_pending, base_url, claude_cmd, timeout, model=model,
                  sidecar_dir=sidecar_dir)


def set_gc_last(board_path: Path, gc_id: str, value: str) -> None:
    """@gc-last eines Items setzen — eigener Schreibpfad (kein Faden-Turn nötig, z.B.
    nach einer Kompaktierung). Unter WRITE_LOCK, mit lost-Guard, atomarer Write —
    dieselbe Disziplin wie jeder andere Schreibpfad. Scheitert still (Meta ist
    nice-to-have; ein Fehler hier darf nichts blockieren)."""
    with board_write_guard(board_path):
        raw = board_path.read_text()
        board = parse_board(raw)
        if lost_total(raw, board) > 0:
            return
        hit = next((it for _s, _n, _c, it in _all_items(board) if it.get("id") == gc_id), None)
        if hit is None:
            return
        hit["gc_last"] = value
        fd, tmp = tempfile.mkstemp(dir=board_path.parent, prefix=".board-")
        with os.fdopen(fd, "w") as f:
            f.write(serialize_board(board))
        os.replace(tmp, board_path)


HIER_PARENT_TURNS = 3  # Kontext runter: Eltern-ID + die letzten N Turns


def hierarchy_context(it: dict, board: dict | None) -> dict:
    """Hierarchie-Kontext eines Items für den Runner-Prompt — die eigentliche Substanz
    des Features: der Agent trägt den Kontext ENTLANG der Kante.

    Runter (`parent`): Eltern-ID/-Titel + die letzten HIER_PARENT_TURNS Turns. Bewusst
    NICHT das `### Arbeitsstand`-Destillat als Primärquelle: gemessen am 23.07. haben nur
    8 von 102 offenen Items einen — „letzte 3 Turns" existiert dagegen immer.
    Hoch (`subs`): Status ALLER Sub-Fäden, bei jedem Eltern-Turn (Owner: „schau mal, wie
    weit sind wir?"). Eine Zeile pro Sub, kein Ping-Pong-Volltext.
    Ohne `board` (Alt-Aufrufer/Tests) fällt alles leer aus — reines Add-on."""
    out: dict = {}
    if board is None:
        return out
    idx = item_index(board)
    if (par := parent_of(it, idx)) is not None:
        turns = [e for e in par.get("thread", []) if e["kind"] in ("ask", "reply")]
        out["parent"] = {"id": par.get("id", ""), "title": par.get("title", ""),
                         "turns": [{"kind": e["kind"], "text": e.get("text", "")}
                                   for e in turns[-HIER_PARENT_TURNS:]],
                         "total_turns": len(turns)}
    if it.get("id") and not it.get("parent"):
        out["subs"] = [{"id": ch.get("id", ""), "title": ch.get("title", ""),
                        "done": bool(ch.get("done")), "status": thread_status(ch),
                        "result": _child_result(ch) if ch.get("done") else ""}
                       for ch in children_of(board, it["id"], idx)]
    return out


def pending_entry(s: str, n: str, c: str | None, it: dict, board: dict | None = None) -> dict:
    """Shape eines for_gc-Items für /api/gc-run + /api/gc-pending. Enthält body
    und thread, damit der Runner den Prompt ohne zweiten Roundtrip bauen kann."""
    return {"hierarchy": hierarchy_context(it, board),
            "addr": {"id": it.get("id", ""), "scope": s, "name": n, "col": c,
                     "title": it["title"], "date": it.get("date", "")},
            "title": it["title"], "session": it.get("session", ""),
            "body": it.get("body", []), "thread": it.get("thread", []),
            # Item-local If-Match for /api/gc-body. Unlike gc-append, a body write
            # REPLACES an old value; without a revision, "under lock" would be
            # atomic but a later agent could still silently overwrite a newer
            # body the owner wrote in the meantime.
            "body_etag": item_body_etag(it.get("body", [])),
            # stages: Phase 3 (stage-tags-PLAN.md, Q5) — der Runner hängt für Dev-Items
            # den Prozess-Stand + Planning-Stups an den Auftrag, ohne zweiten Roundtrip.
            "stages": it.get("stages", []),
            # gc_last für die Kontrakt-Diät: nach ⚙-Compact ("kompaktiert…") schickt
            # build_prompt beim Resume einmalig wieder den Voll-Kontrakt statt Reminder.
            "gc_last": it.get("gc_last", ""),
            "last_ask": it["thread"][-1]["text"] if it["thread"] else ""}


def run_dev_radar() -> dict:
    """Ruft dev_radar.py --json auf (eigener Prozess: langsame Netz-Calls dürfen den
    Server-Thread nicht blockieren, und ein Crash im Radar darf das Board nie mitreißen).
    Liefert IMMER ein Dict — Fehler landen als {"error": ...}, nie als Exception."""
    script = ROOT / "dev_radar.py"
    if not script.is_file():
        return {"error": "dev_radar.py is missing", "items": []}
    try:
        proc = subprocess.run([sys.executable, str(script), "--json"], cwd=GC_ROOT,
                              capture_output=True, text=True, timeout=180,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": "Radar timed out after 180s — are GitLab/GitHub reachable?", "items": []}
    except Exception as exc:  # noqa: BLE001 — der Button darf unter keinen Umständen 500en
        return {"error": f"Failed to start radar: {exc}", "items": []}
    if proc.returncode != 0:
        return {"error": (proc.stderr or "unknown error").strip()[:500], "items": []}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "Radar returned invalid JSON", "items": []}


# ------------------------------------------------- cockpit (Bauplan E2 + E4)
# Datenquellen der Kennzahlen-Zone: nur, was der Server eh weiß (board.md, Journal,
# Run-Registry) plus zwei Log-Dateien read-only. Modul-Globals statt Hardcode im
# Handler, damit Tests sie auf Fixtures umbiegen können (wie gc_runner.JOURNAL_DIR).
JOURNAL_DIR = _p.JOURNAL
# "verfallend" = 2 Tage bevor ein Wait überfällig wird (sweep.WAIT_DECAY_DAYS = 7)
# — Vorwarnung statt Überraschung. Seit 2026-07-22 wandert das Item beim Verfall nicht
# mehr weg, es wird nur überfällig (rot, oben in der Spalte) → WAIT_OVERDUE_DAYS.
WAIT_WARN_DAYS = 5
WAIT_OVERDUE_DAYS = 7  # == sweep.WAIT_DECAY_DAYS (dort die maßgebliche Definition)
DATE_ANY_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
JOURNAL_META_RE = re.compile(r"^run-([0-9a-f]{12})-(\d{8})-\d{6}-[0-9a-f]{4}\.meta\.json$")


def board_kpis(board: dict, today: date) -> dict:
    """Board-Betriebsdaten für die Kennzahlen-Kacheln (E2). "runs_today" zählt ITEMS
    mit mindestens einem Agent-Lauf heute — Quelle @gc-last-Stempel plus noch liegende
    Journal-Metas (fertige Journale werden nach dem Append gelöscht, ein reiner
    Journal-Zähler wäre also fast immer 0). Alle anderen Zähler nur über offene Items."""
    iso = today.isoformat()
    soon = (today + timedelta(days=2)).isoformat()
    warn = (today - timedelta(days=WAIT_WARN_DAYS)).isoformat()
    wait_od = (today - timedelta(days=WAIT_OVERDUE_DAYS)).isoformat()
    k = {"for_owner": 0, "for_gc": 0, "overdue": 0, "due_soon": 0,
         "waits": 0, "waits_decaying": 0, "waits_overdue": 0}
    ran_ids: set[str] = set()
    ran_anon = 0  # gc_last heute, aber Item (noch) ohne id — zählt trotzdem
    for _s, _n, _c, it in _all_items(board):
        if (m := DATE_ANY_RE.search(it.get("gc_last", ""))) and m.group(1) == iso:
            if it.get("id"):
                ran_ids.add(it["id"])
            else:
                ran_anon += 1
        if it["done"] or _s == "cockpit":
            # Run-Zählung oben schließt Erledigtes UND Cockpit-Pseudo-Items bewusst mit
            # ein; die Arbeits-KPIs (for_owner/fällig/waits) zählen nur echte Board-Items —
            # Quick Actions haben ihre eigene Zone, sie sollen die Zähler nicht verzerren.
            continue
        st = thread_status(it)
        if st == "for_owner":
            k["for_owner"] += 1
        elif st == "for_gc":
            k["for_gc"] += 1
        if due := it.get("due", ""):
            if due < iso:
                k["overdue"] += 1
            elif due <= soon:
                k["due_soon"] += 1
        if it.get("wait") or it.get("wait_since"):
            k["waits"] += 1
            since = it.get("wait_since", "")
            if since and since <= wait_od:
                k["waits_overdue"] += 1   # Feedback ist ausgeblieben — nachfassen
            elif since and since <= warn:
                k["waits_decaying"] += 1  # wird in <=2 Tagen überfällig
    if JOURNAL_DIR.is_dir():
        for p in JOURNAL_DIR.iterdir():
            if (m := JOURNAL_META_RE.match(p.name)) and m.group(2) == today.strftime("%Y%m%d"):
                ran_ids.add(m.group(1))
    k["runs_today"] = len(ran_ids) + ran_anon
    k.update(_board_cost(today))
    return k


def _board_cost(today: date) -> dict:
    """Board-Run-Kosten heute/diese Woche aus usage-log.jsonl — Kopfzeilen-Kachel fürs
    Cockpit (Token-Audit 9eea34ffe7c2, Blatt-Frage 3: Default-Profil bleibt vorerst Opus,
    aber die Kosten sollen sichtbar sein statt nur beim Faden-Turn per @gc-last mitzuscrollen).
    Woche = Mo-heute, wie _done_this_week. Reines Reporting — ein fehlendes/kaputtes Log
    darf das Cockpit nie zum Absturz bringen, darum breit try/except wie in log_usage()."""
    import gc_runner
    iso_today = today.isoformat()
    iso_week = (today - timedelta(days=today.weekday())).isoformat()
    cost_today = cost_week = 0.0
    try:
        with open(gc_runner.USAGE_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                d, cost = rec.get("ts", "")[:10], rec.get("cost_usd")
                if not isinstance(cost, (int, float)) or d < iso_week:
                    continue
                cost_week += cost
                if d == iso_today:
                    cost_today += cost
    except OSError:
        pass
    return {"cost_today": round(cost_today, 2), "cost_week": round(cost_week, 2)}


# ── Crew tile: what just ran, what just finished ───────────────────────────────
# The running runs are known to the RUNNING registry (in-memory). The FINISHED ones
# from the last hour live only in the usage log — and that grows (1 MB+), so here we
# deliberately read only the tail of the file and cache it by mtime: /api/etag polls
# every 5 seconds, a full scan every tick would be pure waste.
_FINISHED_CACHE = {"mtime": 0.0, "size": 0, "recs": []}
_FINISHED_TAIL = 400_000


def _usage_tail() -> list[dict]:
    import gc_runner
    try:
        st = os.stat(gc_runner.USAGE_LOG)
    except OSError:
        return []
    if _FINISHED_CACHE["mtime"] == st.st_mtime and _FINISHED_CACHE["size"] == st.st_size:
        return _FINISHED_CACHE["recs"]
    recs = []
    try:
        with open(gc_runner.USAGE_LOG, "rb") as f:
            if st.st_size > _FINISHED_TAIL:
                f.seek(st.st_size - _FINISHED_TAIL)
                f.readline()          # discard the cut-off first line
            for raw in f:
                try:
                    recs.append(json.loads(raw.decode("utf-8")))
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    continue
    except OSError:
        return []
    _FINISHED_CACHE.update({"mtime": st.st_mtime, "size": st.st_size, "recs": recs})
    return recs


def finished_recent(minutes: int = 60, now: datetime | None = None) -> list[dict]:
    """Board runs finished in the last `minutes` — most recent first. Display only;
    a broken log must never take down the header."""
    now = now or datetime.now()
    cut = now - timedelta(minutes=minutes)
    out = []
    for rec in _usage_tail():
        try:
            ts = datetime.strptime(str(rec.get("ts", ""))[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cut or ts > now + timedelta(minutes=5):
            continue
        cost = rec.get("cost_usd")
        # Tokens TOTAL, incl. cache_read: a warm-cache run is cheap but not small — before
        # this the header only carried cost, and a 2M-token run looked like nothing there.
        tok = sum(v for v in (rec.get("input_tokens"), rec.get("cache_read"),
                              rec.get("cache_creation")) if isinstance(v, (int, float)))
        out.append({"id": rec.get("gc_id", ""), "title": rec.get("title", "") or "(no title)",
                    "model": rec.get("model", ""), "ok": bool(rec.get("ok")),
                    "cost": round(cost, 2) if isinstance(cost, (int, float)) else None,
                    "tok": int(tok) or None,
                    "ms": rec.get("duration_ms") or 0,
                    "ago": int((now - ts).total_seconds())})
    out.sort(key=lambda r: r["ago"])
    return out


ACTIONS_FILE = _p.ACTIONS


_LAST_GOOD_INDEX = b""

# The product's own documentation, served to agents from wherever the package
# actually lives. A fresh workspace deliberately does NOT receive copies of
# README.md/ARCHITEKTUR.md: a copied doc is a doc that goes stale on the next
# upgrade, and the first onboarding card would then teach the agent from a lie.
# Serving them instead means there is exactly one copy and it is always the one
# belonging to the running version.
def runner_status(root: Path | None = None) -> tuple[str, str]:
    """Truthful, non-fatal answer to "can this board actually run an agent?".

    Single implementation for both the terminal preflight and `/api/runner-status`;
    the UI needs the same sentence the console prints, because a newcomer who never
    installed Claude Code otherwise only meets a ▶ Agent button that does nothing.
    """
    base = root or GC_ROOT
    wrapper = Path(base) / "tools" / "claude-identities" / "claude-private"
    command = str(wrapper) if wrapper.is_file() else shutil.which("claude")
    if not command:
        return (
            "missing",
            "Claude Code not found on PATH — the board opens, but \u25b6 Agent cannot run yet.",
        )
    try:
        result = subprocess.run(
            [command, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        payload = json.loads(result.stdout or "{}")
        if result.returncode == 0 and payload.get("loggedIn") is True:
            return "ready", "Claude Code is installed and authenticated."
        return (
            "login",
            "Claude Code is installed but not authenticated \u2014 run `claude` once to sign in.",
        )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return "unknown", "Claude Code is installed; authentication could not be verified."


DOC_SOURCES = {
    "readme": ("README.md",),
    "architecture": ("ARCHITEKTUR.md",),
    "changelog": ("CHANGELOG.md",),
}


def read_product_doc(name: str) -> str | None:
    """Resolve a product doc: package dir, then source checkout, then wheel metadata.

    README.md is a packaging `readme` field, so in an installed wheel it exists only
    inside the distribution metadata, not as a file beside the module. ARCHITEKTUR.md
    and CHANGELOG.md ship inside the package. Trying all three sources keeps one code
    path truthful for both a source checkout and an installed wheel.
    """
    candidates = DOC_SOURCES.get(name)
    if not candidates:
        return None
    here = Path(__file__).resolve().parent
    for filename in candidates:
        for base in (here, here.parent):
            candidate = base / filename
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
    if name == "readme":
        try:
            import importlib.metadata as _md

            meta = _md.metadata("superboard")
            text = meta.get_payload() if hasattr(meta, "get_payload") else None
            return text or meta.get("Description") or None
        except Exception:  # noqa: BLE001 — a missing doc is not worth a 500
            return None
    return None


def read_index_html() -> bytes:
    """`index.html` ausliefern — aber nie eine halb geschriebene Datei.

    Warum das nötig ist: index.html wird im laufenden Betrieb überschrieben (Editor eines
    Agenten, `bump.py`), und zwar nicht-atomar — `write_text()` kürzt die Datei erst auf 0
    und schreibt dann. Ein GET genau in diesem Fenster bekommt einen Torso: der Browser
    rendert alles bis zum Abbruch (Kopf, Attention) und lässt den Rest weg — Kennzahlen ohne
    Zahlen, fehlende Bitmap-Symbole, tote Knöpfe. Es sieht aus wie ein Frontend-Bug, ist aber
    ein Lese-Schreib-Rennen; der Tab bleibt so lange kaputt, bis jemand neu lädt.

    Deshalb: nur ausliefern, was auf `</html>` endet. Sonst kurz warten und neu lesen, und
    im Zweifel die letzte bekannt-gute Fassung schicken. Ein Torso wird NIE ausgeliefert.
    """
    global _LAST_GOOD_INDEX
    data = b""
    for _ in range(8):
        try:
            data = (ROOT / "index.html").read_bytes()
        except OSError:
            data = b""
        if data.rstrip().endswith(b"</html>"):
            _LAST_GOOD_INDEX = data
            return data
        time.sleep(0.03)   # Schreibfenster ist Millisekunden kurz — kurz warten reicht
    return _LAST_GOOD_INDEX or data


def load_actions() -> tuple[list[dict], str]:
    """Gültige Quick Actions plus Diagnose; ein defekter Nachbar bleibt isoliert."""
    actions, errors = _registries.load_actions(ACTIONS_FILE)
    return actions, " · ".join(errors)


BOARD_ARCHIVE = GC_ROOT / "inbox" / "board-archive.md"
JETZT_WARN = 5  # Spiegel von CAP.warn in index.html — "Jetzt" zeigt ab >5 den Zähler
ARCHIVE_DAY_RE = re.compile(r"(?m)^## (\d{4}-\d{2}-\d{2})")
DONE_BOX_RE = re.compile(r"(?m)^- \[[xX]\]")


# --- Wesen-Scope (22.07., Faden 09d1203ce11a): „nur die Board-Items, weil ich soll
# nicht so viel an dem Board arbeiten — die machen am meisten Spaß. Aber das ist ja nicht
# die produktive Arbeit. Also die sollen nicht positiv zählen, die anderen schon."
# Ein erledigtes Board-Item ist Zuckerwerk: es darf den ABFLUSS nicht auffüllen, sonst
# belohnt das Wesen ausgerechnet die Ablenkung. Drei Töpfe:
#   prod   — echte Arbeit. Zählt positiv. **Dev (Arbeit) gehört dazu** — das ist
#            echte Projektarbeit, also genau die „produktive Arbeit", die gemeint ist.
#   board  — Zuckerwerk: Dev (Board) + Dev (Tools), also Arbeit AM Board und am eigenen
#            Agent-Tooling. Zählt weder positiv noch negativ, ist aber sichtbar.
#   other  — Personen-Items + Cockpit-Pseudo-Items. Zählen gar nicht (Turn 1).
# Legacy-Namen müssen mit: die Themen hießen bis 22.07. anders, und das Archiv trägt in
# jedem Eintrag die Herkunft von DAMALS („← Code & Tools / Jetzt" — allein 28 Einträge
# in der Woche vor der Umbenennung).
SUGAR_THEME_PREFIXES = ("dev (board", "dev (tools", "am board arbeiten",
                        "code & tools", "board-improvement")
# Für die LAST-Beine (Jetzt-Menge/Alter) bleibt es beim gröberen Schnitt von Stufe 2:
# alles, was in der Dev-View mit eigener Stage-Pipeline lebt, ist dort raus.
DEV_THEME_PREFIXES = ("dev", "am board arbeiten", "code & tools", "board-improvement")


def _is_sugar_theme(name: str) -> bool:
    return name.strip().lower().startswith(SUGAR_THEME_PREFIXES)


def _is_dev_theme(name: str) -> bool:
    return name.strip().lower().startswith(DEV_THEME_PREFIXES)


def _scope_of(kind: str, name: str) -> str:
    if kind != "theme":
        return "other"
    return "board" if _is_sugar_theme(name) else "prod"


ARCHIVE_ORIGIN_RE = re.compile(r"(?m)^- \[[xX]\][^\n]*?←\s*([^\n]+)$")


def _archive_scope(origin: str) -> str:
    """Herkunft einer Archivzeile („Thema / Spalte" bzw. „Person: Name") → Topf."""
    if origin.strip().lower().startswith("person:"):
        return "other"
    return "board" if _is_sugar_theme(origin.split("/")[0]) else "prod"


def _done_since(board: dict, since: str, archive: Path, scope: str = "all") -> int:
    """Erledigte Items ab Datum `since` (ISO) — abgehakte im Board plus Archiv-Abschnitte
    (sweep archiviert nach ~25h, ältere Zählungen leben also überwiegend im Archiv).
    Tages-Granularität reicht für Telemetrie.
    `scope`: "all" (Hi-Score, Telemetrie) | "prod" | "board" | "other" — s. _scope_of."""
    n = sum(1 for kind, name, _c, it in _all_items(board)
            if it["done"] and (it.get("done_at", "") or it.get("date", ""))[:10] >= since
            and (scope == "all" or _scope_of(kind, name) == scope))
    if archive.is_file():
        text = archive.read_text()
        # Abschnittsweise: ## <Archiv-Datum> gruppiert; zählen, was im Fenster ankam.
        parts = ARCHIVE_DAY_RE.split(text)  # [präambel, datum, block, datum, block, …]
        for d, block in zip(parts[1::2], parts[2::2]):
            if d < since:
                continue
            if scope == "all":
                n += len(DONE_BOX_RE.findall(block))
                continue
            # Zeilen OHNE „←"-Herkunft (Archiv-Einträge von vor dem sweep-Format) gelten
            # als produktiv — sie stammen aus der Zeit, als es die Dev-Themen so nicht gab.
            no_origin = len(DONE_BOX_RE.findall(block)) - len(ARCHIVE_ORIGIN_RE.findall(block))
            n += sum(1 for o in ARCHIVE_ORIGIN_RE.findall(block) if _archive_scope(o) == scope)
            if scope == "prod":
                n += no_origin
    return n


def _done_this_week(board: dict, today: date, archive: Path, scope: str = "all") -> int:
    """Freitags-Telemetrie + Hi-Score: erledigte Items seit MONTAG (Kalenderwoche —
    dafür ist der Hi-Score da). Rollende Fenster: `_done_since`."""
    return _done_since(board, (today - timedelta(days=today.weekday())).isoformat(), archive, scope)


def _view_scope_of(kind: str, name: str) -> str:
    """Matrix view bucket: the same split the frontend draws between the To-dos and
    Dev tab. This keeps the DONE hi-score in the header view-accurate instead of
    board-wide (not board-wide once and per-view once)."""
    if kind != "theme":
        return "other"
    return "dev" if _is_dev_theme(name) else "todos"


def _archive_view_scope(origin: str) -> str:
    if origin.strip().lower().startswith("person:"):
        return "other"
    return "dev" if _is_dev_theme(origin.split("/")[0]) else "todos"


def _done_week_views(board: dict, today: date, archive: Path) -> dict[str, int]:
    """{"dev": n, "todos": n} since Monday — ONE archive pass for both buckets (the
    archive is ~700 KB, three extra reads per /api/cockpit would be wasteful).
    Archive lines without a "←" origin predate the Dev themes and count as To-dos."""
    since = (today - timedelta(days=today.weekday())).isoformat()
    out = {"dev": 0, "todos": 0}
    for kind, name, _c, it in _all_items(board):
        if it["done"] and (it.get("done_at", "") or it.get("date", ""))[:10] >= since:
            out[_view_scope_of(kind, name)] = out.get(_view_scope_of(kind, name), 0) + 1
    if archive.is_file():
        parts = ARCHIVE_DAY_RE.split(archive.read_text())
        for d, block in zip(parts[1::2], parts[2::2]):
            if d < since:
                continue
            origins = ARCHIVE_ORIGIN_RE.findall(block)
            for o in origins:
                out[_archive_view_scope(o)] = out.get(_archive_view_scope(o), 0) + 1
            out["todos"] += max(0, len(DONE_BOX_RE.findall(block)) - len(origins))
    return {"dev": out["dev"], "todos": out["todos"]}


def attention_hints(board: dict, today: date, archive: Path | None = None) -> list[dict]:
    """Attention-Zeile V1 (E6): rein regelbasierte Tages-Hinweise, kein LLM.
    Inhalt [Bauplan E6]: Fälliges BEIM NAMEN (die Kacheln zählen nur — hier steht,
    WAS brennt), „Jetzt"-über-Limit pro Thema, freitags das Wochenziel „Jetzt leer"
    [Baustart-Blatt: Inbox-Zero gestrichen] + schlichte Telemetrie-Zeile [R4-★].
    kind steuert nur die Färbung im Frontend: due=rot (hängt), Rest neutral/blau."""
    hints: list[dict] = []
    iso = today.isoformat()
    # Jeder Hinweis trägt sein Thema (`theme`) und, wo es eine Kappung gibt, eine
    # Gruppe (`group`). Grund (Weekend-Modus, 2026-08-07): die Kappung „nur die
    # ersten 3" macht das FRONTEND, nicht mehr der Server — sonst hätte der Server
    # drei Work-Zeilen ausgewählt, die der Weekend-Filter danach wegwirft, und die
    # privaten Fälligkeiten wären unsichtbar dahinter geblieben. Server liefert alle,
    # Client filtert und kappt (renderAttention, ATT_CAP).
    dues: list[tuple[str, str, str, str]] = []  # (due, titel, thema/person, herkunft)
    for _s, _n, _c, it in _all_items(board):
        if not it["done"] and it.get("due") and it["due"] <= iso:
            dues.append((it["due"], it["title"], _n, _s))
    dues.sort()
    for due, title, theme, src in dues:
        days = (today - date.fromisoformat(due)).days
        label = "due today" if days == 0 else f"overdue by {days} day{'s' if days != 1 else ''}"
        hints.append({"kind": "due", "group": "due", "theme": theme, "src": src,
                      "text": f"⏰ {title} — {label}"})
    # Überfällige Waits BEIM NAMEN (2026-07-22): seit dem Umbau bleiben sie in
    # „Wartet auf andere" liegen, statt nach „Jetzt" zurückgeholt zu werden. Ohne diese
    # Zeile wäre die Spalte der einzige Ort, an dem sie auftauchen — und genau dort
    # schaut man nicht hin, solange nichts blinkt.
    od_cut = (today - timedelta(days=WAIT_OVERDUE_DAYS)).isoformat()
    waits: list[tuple[str, str, str, str, str]] = []  # (wait_since, titel, worauf, thema/person, herkunft)
    for _s, _n, _c, it in _all_items(board):
        since = it.get("wait_since", "")
        if not it["done"] and since and since <= od_cut:
            waits.append((since, it["title"], it.get("wait") or "?", _n, _s))
    waits.sort()
    for since, title, on_whom, theme, src in waits:
        days = (today - date.fromisoformat(since)).days
        hints.append({"kind": "due", "group": "wait", "theme": theme, "src": src,
                      "text": f"⏳ {title} — waiting for {on_whom} for {days} days; follow up"})
    for th in board["themes"]:
        n = len([it for it in th["cols"].get("Jetzt", []) if not it["done"]])
        if n > JETZT_WARN:
            hints.append({"kind": "limit", "theme": th["name"], "src": "theme",
                          "text": f"⚠ 'Now' is overflowing in {th['name']} ({n}/{JETZT_WARN})"})
    if today.weekday() == 4:  # Freitag
        jetzt_open = sum(len([it for it in th["cols"].get("Jetzt", []) if not it["done"]])
                         for th in board["themes"])
        goal = "reached ✦" if jetzt_open == 0 else f"{jetzt_open} still open"
        hints.append({"kind": "friday", "text": f"🏁 Friday goal 'Now empty' — {goal}"})
        hints.append({"kind": "friday",
                      "text": f"Completed this week: {_done_this_week(board, today, archive or BOARD_ARCHIVE)} items"})
    return hints


# --- Tages-Triage (E7, Entscheidungen 2026-07-21: Cron 08:30+12:30 + ⟳,
# ersetzt die rohe Jetzt-Liste, Modell Opus für alles). Kein Item-Run: die Triage
# ist ein Systemlauf ohne Faden — Ergebnis lebt als JSON, die UI rendert daraus.
TRIAGE_FILE = _p.JOURNAL / "triage-latest.json"
# Zurückstellen einer Triage-Zeile (2026-08-16, Blatt Q3/Q4): genau zwei Stufen, +1h und
# +1d. Bewusst NICHT als Feld im Item-Body: der Snooze ist reiner Anzeigezustand mit maximal
# einem Tag Halbwertszeit — in board.md wäre jeder Klick ein Commit-Diff an einem Item, das
# sich inhaltlich nicht geändert hat. Hier liegt er neben triage-latest.json, also bei dem,
# was die Anzeige ohnehin steuert, und überlebt trotzdem Reload, Browser und Agent-Läufe.
TRIAGE_SNOOZE_FILE = _p.JOURNAL / "triage-snooze.json"
TRIAGE_SNOOZE_HOURS = (1, 24)
TRIAGE_STATE = {"running": False, "error": "", "failed_slot": ""}
TRIAGE_RAW_FILE = ROOT / "journal" / "triage-last-raw.txt"
TRIAGE_LOCK = threading.Lock()
TRIAGE_SLOTS = ("08:30", "12:30")
TRIAGE_TIMEOUT = 420  # reiner Text-in/JSON-out-Lauf, braucht keine 30 min
TRIAGE_MODEL = "opus"
def item_row(s: str, n: str, c: str | None, it: dict, today: date) -> str:
    """EINE Zeile pro Item: id · Ort · Titel · ★ · Alter · Fälligkeit · Fadenstatus ·
    80 Zeichen Kontext. Das ist die Kompaktdarstellung des Boards — 147 offene Items
    sind so ~7,5k Token statt ~87k für die ganze board.md.

    Zwei Konsumenten, absichtlich dieselbe Zeile: der Triage-Prompt (unten) und
    board_ls.py (Board-Schnellübersicht für Agenten, 2026-07-30). Ändert sich das
    Format, ändert es sich für beide — genau so gewollt."""
    age = ""
    if it.get("date"):
        try:
            age = f" · open {max(0, (today - date.fromisoformat(it['date'])).days)}d"
        except ValueError:
            pass
    due = f" · due {it['due']}" if it.get("due") else ""
    st = {"for_owner": " · GC reply waiting for owner", "for_gc": " · agent's turn"}.get(thread_status(it), "")
    mark = " · ★" if it.get("mark") else ""
    done = " · ✓done" if it.get("done") else ""
    wo = f"{n}/{c}" if s == "theme" else f"Person:{n}"
    kurz = (it.get("body") or [""])[0][:80]
    kurz = f" · {kurz}" if kurz and not kurz.startswith("action:") else ""
    return f"- id={it.get('id', '?')} [{wo}] {it['title']}{mark}{done}{age}{due}{st}{kurz}"


def _triage_prompt(board: dict, today: date) -> str:
    """Kompakte Item-Liste + strikte JSON-Aufgabe. Der Agent braucht keine Tools —
    alles Nötige steht im Prompt (schneller, billiger, kein Permission-Rauschen)."""
    items = "\n".join(
        item_row(s, n, c, it, today)
        for s, n, c, it in _all_items(board)
        if not it["done"] and s != "cockpit"
    )
    return f"""You are the daily triage for {_cfg.OWNER}'s to-do board ({today.isoformat()}). All open items are listed below. Classify a SELECTION into exactly 3 groups — the value lies in your judgment, not in completeness:

- "quick": requires only minimal input — he can complete or move it forward within minutes (max 8)
- "stale": has been sitting conspicuously long — he should briefly say what should happen to it (max 8)
- "deep": if he makes time today, THIS would matter (max 8, most important first)

The UI shows only the top 3 in each group — the rest is the day's reserve queue. Therefore sort
STRICTLY by importance WITHIN each group: positions 1–3 are what he should genuinely see first;
4–8 are good candidates for later. Prefer 8 solid entries over 3 good ones plus 5 fillers.

Rules: Give each entry one short, concrete classification ("note", 1 sentence: why now / what to do — address the owner directly in the second person). Deliberately include underrepresented themes and long-neglected work instead of only the loudest items. Items with a waiting GC reply generally belong in quick (just read and respond). Optional "footnotes": at most 2 system-level observations about the board as a whole (patterns, imbalances) — only when you genuinely notice something.

Respond ONLY with this JSON, without a Markdown fence or surrounding text:
{{"quick": [{{"id": "…", "note": "…"}}], "stale": […], "deep": […], "footnotes": ["…"]}}

Items:
{items}"""


def _close_json(text: str) -> str:
    """Close truncated JSON without trying to repair its actual values."""
    stack: list[str] = []
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
        elif char in "]}" and stack:
            stack.pop()
    if in_string:
        text += '"'
    text = re.sub(r",\s*$", "", text)
    return text + "".join(reversed(stack))


def _extract_json(text: str) -> dict:
    """Extract JSON, tolerating fences, prose and missing closing brackets."""
    t = text.strip()
    if "```" in t:
        t = re.sub(r"^.*?```(?:json)?\s*", "", t, count=1, flags=re.S)
        t = t.split("```")[0]
    start, end = t.find("{"), t.rfind("}")
    if start < 0:
        raise ValueError("No JSON in the reply")
    if end > start:
        try:
            return json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            pass
    return json.loads(_close_json(t[start:].rstrip()))


def _dump_triage_raw(text: str) -> str:
    """Persist the latest failed reply so the visible error is diagnosable."""
    if not text.strip():
        return ""
    try:
        TRIAGE_RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRIAGE_RAW_FILE.write_text(
            f"# {time.strftime('%Y-%m-%dT%H:%M:%S')} · failed triage reply\n{text[:40000]}",
            encoding="utf-8",
        )
    except OSError:
        return ""
    relative = (TRIAGE_RAW_FILE.name if ROOT not in TRIAGE_RAW_FILE.parents
                else TRIAGE_RAW_FILE.relative_to(ROOT))
    return f" · raw: {relative}"


def start_triage(board_path: Path, claude_cmd: str, model: str = TRIAGE_MODEL) -> bool:
    """Triage-Lauf als Daemon-Thread. False = läuft schon. Ergebnis atomar nach
    TRIAGE_FILE; Fehler landen sichtbar in TRIAGE_STATE['error'] (Zone zeigt sie),
    der letzte gute Stand bleibt dabei liegen — nie stumm, nie kaputtschreiben."""
    with TRIAGE_LOCK:
        if TRIAGE_STATE["running"]:
            return False
        TRIAGE_STATE["running"] = True

    def work() -> None:
        import gc_runner
        out: dict = {}
        try:
            board = parse_board(board_path.read_text())
            out = gc_runner.spawn_claude(_triage_prompt(board, date.today()), "",
                                         claude_cmd, TRIAGE_TIMEOUT, model=model)
            if not out["ok"]:
                raise RuntimeError(out["raw_error"] or out["reply"] or "Run failed")
            data = _extract_json(out["reply"])
            groups = data.get("groups") if isinstance(data.get("groups"), dict) else data
            if not any(groups.get(g) for g in ("quick", "stale", "deep")):
                raise ValueError("Reply contains no group entries")
            payload = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model,
                       "groups": {g: list(groups.get(g) or []) for g in ("quick", "stale", "deep")},
                       "footnotes": list(data.get("footnotes") or [])[:2]}
            TRIAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=TRIAGE_FILE.parent, prefix=".triage-")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, TRIAGE_FILE)
            TRIAGE_STATE["error"] = ""
            TRIAGE_STATE["failed_slot"] = ""
        except Exception as e:  # noqa: BLE001 — Fehler sichtbar machen, nie crashen
            raw = _dump_triage_raw(out.get("reply") or out.get("raw_error") or "")
            TRIAGE_STATE["error"] = f"{str(e)[:260]}{raw}"
            TRIAGE_STATE["failed_slot"] = _last_slot(time.localtime())
            print(f"superboard: triage run failed: {e}", file=sys.stderr)
        finally:
            TRIAGE_STATE["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return True


def _last_slot(now: time.struct_time) -> str:
    """Jüngster heute schon vergangener Triage-Slot als ISO-Zeitpunkt ('' = noch keiner).
    Selbstheilend nach Mac-Sleep: der Cron vergleicht 'generated < Slot' — ein verpasster
    08:30-Takt wird beim nächsten Tick nachgeholt, egal wann der Mac aufwacht."""
    today = time.strftime("%Y-%m-%d", now)
    hhmm = time.strftime("%H:%M", now)
    passed = [s for s in TRIAGE_SLOTS if s <= hhmm]
    return f"{today}T{passed[-1]}:00" if passed else ""


def triage_snoozed(now: datetime | None = None) -> dict[str, str]:
    """item-id → ISO-Zeitpunkt, bis zu dem die Zeile aus der Triage verschwindet.
    Abgelaufene Einträge fallen schon beim LESEN raus — es braucht keinen Aufräumjob und
    ein kaputtes/fehlendes File ist gleichbedeutend mit „nichts zurückgestellt"."""
    now = now or datetime.now()
    try:
        data = json.loads(TRIAGE_SNOOZE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in data.items():
        try:
            if datetime.fromisoformat(str(val)) > now:
                out[str(key)] = str(val)
        except (TypeError, ValueError):
            continue
    return out


def triage_snooze(item_id: str, hours: float) -> str:
    """Zeile zurückstellen; gibt den neuen Zeitpunkt zurück. Atomar geschrieben, weil das
    Cockpit die Datei jede Minute liest."""
    now = datetime.now()
    cur = triage_snoozed(now)
    cur[item_id] = (now + timedelta(hours=hours)).isoformat(timespec="minutes")
    TRIAGE_SNOOZE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRIAGE_SNOOZE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    tmp.replace(TRIAGE_SNOOZE_FILE)
    return cur[item_id]


def triage_status() -> dict:
    """Zonen-Zustand für /api/cockpit: letzter Stand + running/error + zurückgestellte
    Zeilen. stale, wenn der jüngste vergangene Slot neuer ist als der Stand (Cron holt
    das gleich nach)."""
    st = {"present": False, "generated": "", "model": "", "groups": {}, "footnotes": [],
          "running": TRIAGE_STATE["running"], "error": TRIAGE_STATE["error"], "stale": False,
          "snoozed": triage_snoozed()}
    if TRIAGE_FILE.is_file():
        try:
            data = json.loads(TRIAGE_FILE.read_text())
            st.update({"present": True, "generated": data.get("generated", ""),
                       "model": data.get("model", ""), "groups": data.get("groups", {}),
                       "footnotes": data.get("footnotes", [])})
            slot = _last_slot(time.localtime())
            st["stale"] = bool(slot) and st["generated"] < slot
        except (json.JSONDecodeError, OSError) as e:
            st["error"] = st["error"] or f"triage-latest.json cannot be read: {e}"
    return st


def _triage_due(slot: str, generated: str) -> bool:
    """Allow at most one automatic attempt for each scheduled slot."""
    return (bool(slot) and generated < slot and not TRIAGE_STATE["running"]
            and TRIAGE_STATE["failed_slot"] != slot)


def _workspace_ever_ran_an_agent(board_path: Path) -> bool:
    """Hat in DIESEM Workspace je ein Lauf stattgefunden? Signal: eine Faden-Datei.

    Nur dafür da, den einzigen unbeaufsichtigten Token-Verbrenner (den Triage-Takt)
    von einer FRISCHEN Installation fernzuhalten. Gefunden am 22.08. im Test-Rig: ein
    frisch installiertes Board stiess beim Start sofort einen Opus-Triage-Lauf an,
    ohne dass der Besitzer irgendetwas geklickt hatte — genau das, was README und
    ARCHITEKTUR ausschliessen. Der Cockpit-Knopf startet Triage weiterhin sofort;
    automatisch laeuft sie erst, wenn der Workspace wirklich benutzt wird."""
    try:
        return any((board_path.parent / "gc-threads").glob("*.md"))
    except OSError:
        return True


def triage_cron(board_path: Path, claude_cmd: str) -> None:
    """Grundtakt 08:30/12:30 [21.07., Frage ①]: minütlicher Tick, angestoßen wird
    nur, wenn der Stand älter ist als der jüngste vergangene Slot. Läuft im Server-
    Prozess (LaunchAgent) — Sleep verpasst Ticks, der Vergleich holt sie nach.

    Auf einem unbenutzten Workspace schweigt der Takt ganz (s. oben)."""
    while True:
        try:
            slot = _last_slot(time.localtime())
            if slot and _workspace_ever_ran_an_agent(board_path):
                generated = ""
                if TRIAGE_FILE.is_file():
                    try:
                        generated = json.loads(TRIAGE_FILE.read_text()).get("generated", "")
                    except (json.JSONDecodeError, OSError):
                        pass
                if _triage_due(slot, generated):
                    print(f"superboard: triage cron — slot {slot} due (last: {generated or 'never'})")
                    start_triage(board_path, claude_cmd)
        except Exception as e:  # noqa: BLE001 — der Takt darf den Server nie mitreißen
            print(f"superboard: triage cron: {e}", file=sys.stderr)
        time.sleep(60)


# --- Wesen-Schwellen (21.07., Blatt „Wesen-Kalibrierung", Faden a21361fd1e95).
# Bewusst ENTKOPPELT von der WIP-Kappe JETZT_WARN (=5): die Kappe ist ein Ziel, das der Owner
# bewusst überschreitet; das Wesen soll erst reden, wenn es real aus dem Ruder läuft.
# V1 koppelte beides und stand dadurch dauerhaft auf KOPF PLATZT.
WESEN_JETZT = 15   # Owner: „15. sonst ist es so oft gekippt"
WESEN_ALTER = 14   # Tage. Die Aging-Kante an der KARTE bleibt bei 7 (AGE_MAX in index.html):
                   # die Karte sagt leise „wird alt", das Wesen laut „liegt jetzt echt".
# (Die alte Weich-Schwelle WESEN_SOFT = 0.8 ist am 07.08. entfallen — „angeknackst" ist
#  jetzt kein Sonderfall mehr, sondern fällt aus dem stetigen Score darunter.)
# --- Graduierter Last-Score (07.08., Blatt „Wesen-Logik", Faden b8ab15e70af0).
# Vorher waren beide Beine BINÄR (hart/weich). Effekt in echten Daten (13 Tage Historie,
# 21.07.–07.08.): 12 von 13 Tagen nicht healthy, weil EIN einziges uraltes Jetzt-Item das
# Alters-Bein dauerhaft hart hielt — und ein Häkchen bewegte sichtbar gar nichts, solange
# es keine Schwelle riss. Jetzt: ein stetiger Score, aus dem die Stufe fällt.
#   menge = Jetzt-Menge / WESEN_JETZT
#   alter = mehrheitlich ANZAHL überalterter Items, ein Rest für das älteste Einzelitem
# Owner zu Q3: „anzahl über schwelle ist besser als 1 absolut. aber 1 absolut alt ist auch
# schlecht und darf negativ sein" — genau dieses Verhältnis steckt in WESEN_ALT_ABS.
WESEN_ALT_N = 3          # so viele überalterte Jetzt-Items = volles Alters-Bein
WESEN_ALT_ABS = 0.35     # Anteil des Alters-Beins, den das ÄLTESTE Einzelitem allein trägt
WESEN_W_MENGE = 0.55     # Menge wiegt etwas schwerer — sie ist das Bein, das EIN Häkchen
WESEN_W_ALTER = 0.45     # sofort bewegt (0.55/15 ≈ 3,7 Punkte pro Häkchen)
WESEN_LEG_CAP = 1.6      # ASYMPTOTE, kein Deckel mehr (s. _soft_cap, Runde 2 07.08.)
# --- Weiche Sättigung (07.08. Q1=B). Der harte `min(1.6, x)` hat oben ein PLATEAU
# erzeugt: gemessen an der echten Historie waren 30, 26 und 24 Jetzt-Items derselbe Score,
# und das Alters-Bein stand seit dem 22.07. am Anschlag. Owner: „22 items = immer schlimmste
# stufe, egal ob ich gerade von 30 auf 22 runtergekommen bin? zu hart." Unterhalb der
# Schwelle (x ≤ 1) bleibt alles LINEAR wie bisher; darüber wächst es gedämpft weiter und
# nähert sich WESEN_LEG_CAP nur asymptotisch. Damit ist jeder Schritt oben noch sichtbar,
# ohne dass ein Ausreißer die Skala sprengt.
# --- Richtungs-Bein (07.08. Q2=C, mit Vorbehalt „nur wenn es ein paar Zeilen in einem
# bestehenden Modul auf einer eh bestehenden Datenquelle sind"). Genau das ist der Fall:
# journal/wesen-history.jsonl wird ohnehin täglich geschrieben (_wesen_snapshot) und
# ohnehin gelesen — kein neuer Schreibpfad, keine neue Abhängigkeit, kein Extra-Roundtrip.
# Ersetzt das Kurz-Gedächtnis aus Runde 1, das das FALSCHE VORZEICHEN hatte: es zog den
# heutigen Score zum Wochenmittel und schob damit bei VERBESSERUNG nach oben — Runterkommen
# wurde aktiv bestraft. Jetzt zählt der Abstand zum 7-Tage-Mittel (Q4=C) mit klarem
# Vorzeichen: besser als die Woche = Entlastung, schlechter = Aufschlag.
WESEN_DIR_DAYS = 7        # Vergleichsfenster (Q4=C: Mittel der letzten 7 Tage)
WESEN_DIR_W = 0.6         # wie stark der Abstand zum Mittel durchschlägt
WESEN_DIR_RELIEF = 0.35   # Entlastung max ≈ zwei Bänder (Q3=B: „bis zu zwei Stufen")
WESEN_DIR_PENALTY = 0.15  # Aufschlag max ≈ ein Band — bewusst ASYMMETRISCH: der Hebel ist
                          # die Entlastung; schnelles Schlechterwerden darf warnen, aber
                          # nicht selbst zum Alarm werden (die Last sagt das schon).
WESEN_DIR_MIN_ROWS = 2    # unter zwei Vergleichstagen ist „Richtung" geraten, nicht gemessen
# Stufen auf dem Score. Bänder ~0,15–0,20 breit ⇒ 4–5 Häkchen wechseln die Stufe, jedes
# einzelne bewegt Prozentzahl und Farbe sichtbar (Owner: „spürbar bei jedem Abhaken").
WESEN_BAND_MUEDE = 0.50   # MÜDE      — leiseste Vorstufe, neu
WESEN_BAND_ACHE = 0.65    # KOPFSCHMERZEN
WESEN_BAND_POCHEN = 0.85  # KOPF POCHT — zwischen Kopfschmerzen und Platzen, neu
WESEN_BAND_TOP = 1.05     # KOPF RAUCHT / KOPF PLATZT
# Zwei harte Leitplanken für ALLES, was aus der Historie kommt (Richtungs-Bein) — damit der
# Alarm NICHT an einer gitignored Journaldatei hängt:
#   1. fehlt/kaputt die Datei ⇒ gar kein Effekt (frischer Checkout rechnet nur „heute")
#   2. der Effekt ist gedeckelt (WESEN_DIR_RELIEF/_PENALTY) ⇒ die Historie kann die Stufe
#      verschieben, aber nie im Alleingang aus einem ruhigen Board einen Alarm machen
# --- Velocity-Bein (22.07., Faden 09d1203ce11a): „je länger die Inbox von Jetzt ist,
# desto schlechter — da kommt aber auch Velocity ein bisschen rein: wie viel kommt rein,
# was geht raus". Saldo statt Verhältnis: „6 mehr rein als raus" ist erklärbar, die alte
# Regel (`inflow > 2 × outflow`) feuerte bei ruhigem Board schon ab 4 Neuen und bei
# fleißigem Board nie. Beide Seiten zählen nur PRODUKTIVE Items (s. _scope_of).
WESEN_VELO_GAP = 4    # Zufluss-Überhang (7D), ab dem es kippt
WESEN_VELO_MIN = 4    # …aber erst ab so vielen Neuen überhaupt (Rauschschutz)
# Zuckerregel: so viele Board-/Dev-Erledigungen in 7 Tagen machen das Wesen ÜBERFÜTTERT,
# sobald sie den produktiven Abfluss überholen.
WESEN_ZUCKER = 3
# --- „KOPF RAUCHT" (30.07., Faden c5efd60871e8): „viel los, aber auch viel gemacht …
# aktuell habe ich die ganze Zeit dass der Kopf platzt". Volle Last (beide Beine hart) sagt
# noch nichts darüber, ob der Owner sich dagegen stemmt oder untergeht. Läuft der produktive
# Abfluss mit, ist das kein Platzen, sondern Maloche — eigener Zustand statt Dauer-Alarm.
WESEN_RAUCHT_OUT = 8      # produktive Erledigungen (7D), ab denen „Last mit Fahrt" gilt
# …und der Abfluss muss wenigstens grob mit dem Zufluss mithalten. Bewusst NICHT „≥ Zufluss":
# in einer echten Woche (30.07.: RAUS 24 · REIN 36) wäre genau die Woche, die sich am
# meisten nach Maloche anfühlt, wieder PLATZT geworden. Erst wenn der Zufluss den Abfluss
# klar abhängt, ist es kein Rauchen mehr, sondern Untergehen. Wert ist geraten wie alle
# Wesen-Schwellen — gehört zur Nachkalibrierung ab 11.08.
WESEN_RAUCHT_RATIO = 0.6


def _soft_cap(x: float, knee: float = 1.0, cap: float = WESEN_LEG_CAP) -> float:
    """Weiche Sättigung statt hartem Deckel (07.08. Q1=B).

    Bis `knee` (= genau auf der Schwelle) exakt linear — unterhalb der Schwelle ändert
    sich gegenüber Runde 1 also GAR NICHTS. Darüber wächst der Wert gedämpft weiter und
    nähert sich `cap`, ohne ihn je zu erreichen. Der Anstieg ist an der Knickstelle
    stetig (Ableitung 1), es gibt also keinen Sprung, an dem ein Häkchen plötzlich mehr
    oder weniger wert wäre. Wirkung in echten Zahlen (WESEN_JETZT=15): 22/26/30 Jetzt-Items
    ergaben vorher dreimal 1.60 — jetzt 1.32/1.42/1.49, das Runterkommen ist sichtbar."""
    if x <= knee:
        return x
    head = cap - knee
    return knee + head * (1.0 - math.exp(-(x - knee) / head))


def _wesen_legs(n: int, alt_count: int, oldest_days: int,
                soft: bool = True) -> tuple[float, float]:
    """Die zwei Last-Beine als stetige Größen (1.0 = genau auf der Schwelle).

    `soft=False` liefert die UNGEDÄMPFTEN Rohverhältnisse. Die braucht das Richtungs-Bein:
    gesättigt ist der Unterschied zwischen 30 und 22 Jetzt-Items nur noch 0.09 Punkte, roh
    aber 0.53 — der Owner nimmt aber genau diese acht abgeräumten Items wahr, nicht die
    gestauchte Skala. Also: STAND misst gesättigt (kein Ausreißer sprengt die Skala),
    BEWEGUNG misst roh (das Runterkommen bleibt in voller Größe spürbar).

    Das Alters-Bein ist bewusst ZWEIGETEILT: der Löwenanteil hängt an der ANZAHL
    überalterter Jetzt-Items, ein kleinerer Rest (WESEN_ALT_ABS) am ältesten Einzelitem.
    Dadurch bleibt ein einzelner Uralt-Brocken spürbar negativ, kann das Bein aber nicht
    mehr im Alleingang anschlagen lassen — genau der Fall, der das Wesen seit dem 15.07.
    an EINEM Item festgenagelt hat (Item „Enablement-Workstream", wartet strukturell auf
    einen Kollegen im Urlaub)."""
    f = _soft_cap if soft else (lambda x: x)
    menge = f(n / WESEN_JETZT)
    alter_n = f(alt_count / WESEN_ALT_N)
    alter_abs = f(oldest_days / WESEN_ALTER)
    return menge, (1 - WESEN_ALT_ABS) * alter_n + WESEN_ALT_ABS * alter_abs


def _wesen_strain(menge: float, alter: float) -> float:
    return WESEN_W_MENGE * menge + WESEN_W_ALTER * alter


def _wesen_row_load(row: dict) -> float | None:
    """Ungedämpfte Rohlast einer Historienzeile — die Vergleichsgröße des Richtungs-Beins.

    Immer NEU GERECHNET aus den Rohfeldern, nie aus dem mitgeschriebenen `strain`: die
    Formel ändert sich (Runde 1 harter Deckel → Runde 2 weiche Sättigung), die Rohzahlen
    nicht. Alte Scores direkt zu mitteln hieße, heutige Zahlen gegen die Ergebnisse einer
    anderen Formel zu halten — und weil der harte Deckel oben systematisch höher lag, käme
    dabei eine dauerhafte Schein-Entlastung heraus.
    `jetzt_over_14d` gibt es erst seit 07.08. — fehlt es, dient `jetzt_over_7d` als
    Näherung (überschätzt das Alters-Bein leicht, wächst sich binnen einer Woche aus)."""
    if "jetzt" not in row or "oldest_days" not in row:
        return None
    alt = row.get("jetzt_over_14d", row.get("jetzt_over_7d", 0))
    return _wesen_strain(*_wesen_legs(row["jetzt"], alt, row["oldest_days"], soft=False))


def _wesen_direction(today: date, load_today: float) -> tuple[float, float | None]:
    """Richtungs-Bein: wohin bewegt sich die Last? (07.08. Q2=C / Q3=B / Q4=C)

    Vergleicht die heutige Rohlast mit dem Mittel der letzten WESEN_DIR_DAYS Tage. Ist es
    heute besser als im Wochenschnitt, wird der Score ENTLASTET (bis zu zwei Bänder); ist
    es schlechter, kommt ein kleinerer Aufschlag drauf. Damit fühlt sich „ich komme gerade
    von 30 auf 22 runter" anders an als „ich sitze seit zwei Wochen auf 22".

    Rückgabe: (adjust, mittel) — `adjust` wird vom Score ABGEZOGEN, `mittel` ist None,
    wenn es keine verwertbare Historie gibt (dann ist `adjust` 0.0 und alles bleibt beim
    reinen Heute-Wert). Darf nie werfen: die Kachel ist wichtiger als die Statistik."""
    try:
        if not WESEN_HISTORY.is_file():
            return 0.0, None
        iso_today, vals = today.isoformat(), []
        # Datums-Boden: nach einer Board-Pause soll nicht die Stimmung von vor drei Wochen
        # als „Woche" durchgehen. Lücken im Fenster sind ok, es wird über das gemittelt,
        # was da ist — nur ein einzelner Vergleichstag reicht nicht (WESEN_DIR_MIN_ROWS).
        iso_floor = (today - timedelta(days=WESEN_DIR_DAYS)).isoformat()
        for line in WESEN_HISTORY.read_text(encoding="utf-8").splitlines()[-30:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not (iso_floor <= row.get("date", "") < iso_today):
                continue
            s = _wesen_row_load(row)
            if s is not None:
                vals.append(s)
        if len(vals) < WESEN_DIR_MIN_ROWS:
            return 0.0, None
        mittel = sum(vals) / len(vals)
        # Vorzeichen: mittel > heute ⇒ es geht RUNTER ⇒ positiver adjust ⇒ Score sinkt.
        adjust = max(-WESEN_DIR_PENALTY,
                     min(WESEN_DIR_RELIEF, WESEN_DIR_W * (mittel - load_today)))
        return adjust, mittel
    except Exception:  # noqa: BLE001 — die Kachel darf nie an der Statistik sterben
        return 0.0, None


def _wesen_trend(state: str, jetzt_now: int, today: date) -> str:
    """Historischer Kontext aus journal/wesen-history.jsonl (30.07.: „eine dynamische
    Komponente … bleibt es länger da? addiert sich das auf?"). Zwei billige Signale:
      TAG n  — der wievielte Tag in Folge in DIESEM Zustand (heutige Zeile zählt mit)
      JETZT ↑/↓k — Bewegung der Jetzt-Menge gegenüber dem ältesten Eintrag der letzten 7 Tage
    Darf nie werfen und nie den Zustand ändern — reine Dekoration der Warum-Zeile."""
    try:
        if not WESEN_HISTORY.is_file():
            return ""
        rows = []
        for line in WESEN_HISTORY.read_text(encoding="utf-8").splitlines()[-30:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        iso_today = today.isoformat()
        past = [r for r in rows if r.get("date", "") < iso_today]
        streak = 1
        for r in reversed(past):
            if r.get("state") == state:
                streak += 1
            else:
                break
        bits = [f"DAY {streak}"] if streak > 1 else []
        iso7 = (today - timedelta(days=7)).isoformat()
        window = [r for r in past if r.get("date", "") >= iso7 and "jetzt" in r]
        if window:
            d = jetzt_now - window[0]["jetzt"]
            if d:
                bits.append(f"NOW {'↑' if d > 0 else '↓'}{abs(d)} (7D)")
        return (" · " + " · ".join(bits)) if bits else ""
    except Exception:  # noqa: BLE001 — Kachel darf nie 500en
        return ""


# ================================================================== Heute-Zone Stufe 1
# Pflicht-Rituale (Konzept: design-proposals/heute-zone-konzept.md, Baufreigabe 21.07.,
# v0.12.0). KEINE board.md-Items — Definition in rituale.json (Name/Rhythmus/Deadline/
# Proof-Prompt), Tagesstatus + Proofs im append-only Journal journal/rituale.jsonl
# (gitignored wie journal/ generell). "Kein Archiv-Spam" (Bens Sorge): eine Zeile pro
# Event (done/snooze/override), nicht pro Tag re-geschrieben.
RITUALE_FILE = _p.RITUALS
RITUAL_JOURNAL = _p.JOURNAL / "rituale.jsonl"
GATE_OVERRIDE_SILENCE_MIN = 30  # Override beruhigt DAS GATE (alle Rituale) für diese Zeit
RITUAL_SNOOZE_HOURS = 1         # +1h, max 1× pro Ritual/Zyklus (Tag bzw. Woche)
# Serialisiert Read-Check-Append in _ritual_done() — ThreadingHTTPServer bedient Requests
# parallel; ohne Lock könnten zwei fast gleichzeitige POSTs (Doppelklick am proof-losen
# Direktpfad) beide "noch kein done im Zyklus" sehen und je ein Event anhängen (Sub-Review
# 22.07. gefunden, mittlerer Schweregrad).
RITUAL_LOCK = threading.Lock()


def ritual_now() -> datetime:
    """Injectable Uhr — Tests biegen `server.ritual_now` auf eine feste Zeit um, ohne
    datetime.now() selbst zu patchen. Reine Funktionen (rituale_status, ritual_instance,
    _ritual_cycle) nehmen zusätzlich ein optionales `now`-Argument für direkte Unit-Tests."""
    return datetime.now()


def load_rituale(path: Path | None = None) -> dict:
    """Gültige Rituale; einzelne kaputte Definitionen werden ausgelassen."""
    config, _errors = _registries.load_rituals(path or RITUALE_FILE)
    return config


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # eine kaputte Zeile darf den Rest des Journals nicht ziehen
    return out


def _append_jsonl(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _ritual_cycle(cfg: dict, now: datetime) -> tuple[datetime, datetime, str]:
    """(appear_dt, deadline_dt, cycle_key) für den Zyklus, in dem `now` gerade steckt.

    daily:  Zyklus = Kalendertag. appear = cfg["appears"] (Default 06:00, Hidden-Grenze —
            ein daily-Ritual kann per "appears" einen späteren Sichtbarkeitsstart setzen),
            deadline = cfg["deadline"].
    weekly: Zyklus = das Di-17:00–Mi-17:00-Fenster (Mock-Review 21.07.: „längere
            Deadline"). appear = jüngster Di 17:00 <= now, deadline = appear + 1 Tag.
    cycle_key verankert Journal-Events (done/snooze) an genau diesem Zyklus — ein Tages-
    bzw. Wochenwechsel startet automatisch frisch (kein manuelles Zurücksetzen nötig).
    Kein tagesübergreifendes Daily-Modell: ein "overdue"-Zyklus endet hart um 00:00 (neuer
    Zyklus startet "hidden"). Bewusst akzeptiert (Sub-Review 22.07.), kein offenes Problem hier."""
    if cfg.get("kind") == "weekly":
        ap, dl = cfg["appears"], cfg["deadline"]
        ahh, amm = map(int, ap["time"].split(":"))
        delta = (now.weekday() - ap["weekday"]) % 7
        appear_dt = datetime.combine(now.date() - timedelta(days=delta), dtime(ahh, amm))
        if now < appear_dt:
            appear_dt -= timedelta(days=7)
        dhh, dmm = map(int, dl["time"].split(":"))
        day_delta = (dl["weekday"] - ap["weekday"]) % 7
        deadline_dt = datetime.combine(appear_dt.date() + timedelta(days=day_delta), dtime(dhh, dmm))
        return appear_dt, deadline_dt, appear_dt.date().isoformat()
    # daily
    hh, mm = map(int, cfg["deadline"].split(":"))
    ahh, amm = map(int, cfg.get("appears", "06:00").split(":"))
    today_d = now.date()
    return datetime.combine(today_d, dtime(ahh, amm)), datetime.combine(today_d, dtime(hh, mm)), today_d.isoformat()


def ritual_instance(rid: str, cfg: dict, now: datetime, journal: list[dict], active_from: str) -> dict:
    """Ein Ritual → seine heutige/aktuelle Instanz. Status-Reihenfolge: hidden (vor der
    Aktivierung ODER außerhalb des Sichtbarkeitsfensters) > done > overdue > open.
    Snooze verschiebt NUR die effektive Deadline dieses einen Rituals (+1h ab Snooze-Zeit);
    das globale Gate-Silence (Override) lebt separat in gate_silence_active()."""
    appear_dt, deadline_dt, cycle = _ritual_cycle(cfg, now)
    out = {"id": rid, "title": cfg.get("title", rid), "kind": cfg.get("kind", "daily"),
           "proof": cfg.get("proof", "single"), "prompt": cfg.get("prompt", ""),
           "deadline": deadline_dt.isoformat(timespec="minutes"),
           "status": "hidden", "snoozed_until": "", "done_at": ""}
    if active_from and now.date().isoformat() < active_from:
        return out  # Rituale eingeführt, aber noch nicht aktiv (Rollout-Tag)
    events = [e for e in journal if e.get("ritual") == rid and e.get("cycle") == cycle]
    done_ev = next((e for e in events if e.get("kind") == "done"), None)
    snooze_ev = next((e for e in events if e.get("kind") == "snooze"), None)
    effective_deadline = deadline_dt
    if snooze_ev:
        try:
            snoozed_until = datetime.fromisoformat(snooze_ev["new_deadline"])
            effective_deadline = max(effective_deadline, snoozed_until)
            out["snoozed_until"] = snoozed_until.isoformat(timespec="minutes")
        except (KeyError, ValueError):
            pass
    if cfg.get("kind") == "weekly":
        visible = appear_dt <= now < effective_deadline
    else:
        visible = now >= appear_dt  # hidden nur bis appear_dt (Default 06:00); ab dann sichtbar bis Tagesende
    if not visible:
        return out
    if done_ev:
        out["status"] = "done"
        out["done_at"] = done_ev.get("ts", "")
    elif now >= effective_deadline:
        out["status"] = "overdue"
    else:
        out["status"] = "open"
    return out


def rituale_status(now: datetime | None = None, config: dict | None = None) -> list[dict]:
    """Alle Ritual-Instanzen für /api/rituale + wesen_status (hungry-Check)."""
    now = now or ritual_now()
    cfg = config if config is not None else load_rituale()
    active_from = cfg.get("active_from", "")
    journal = _read_jsonl(RITUAL_JOURNAL)
    return [ritual_instance(rid, rcfg, now, journal, active_from)
            for rid, rcfg in (cfg.get("rituale") or {}).items()]


def gate_silence_active(now: datetime | None = None) -> bool:
    """True, wenn ein "Trotzdem"-Override das Gate gerade beruhigt (GATE_OVERRIDE_SILENCE_MIN
    ab dem letzten Override-Event) — global fürs Gate, nicht pro Ritual."""
    now = now or ritual_now()
    overrides = [e for e in _read_jsonl(RITUAL_JOURNAL)
                 if e.get("kind") == "override" and e.get("gate") == "ritual"]
    if not overrides:
        return False
    try:
        last = max(datetime.fromisoformat(e["ts"]) for e in overrides if e.get("ts"))
    except ValueError:
        return False
    return (now - last) < timedelta(minutes=GATE_OVERRIDE_SILENCE_MIN)


def wesen_status(board: dict, today: date, archive: Path | None = None,
                  rituale: list[dict] | None = None, gate_silenced: bool = False) -> dict:
    """Wesen-Zustand + historischer Kontext. Die Regeln stehen in `_wesen_core`; hier kommt
    nur die Verlaufs-Dekoration (`_wesen_trend`) an die Warum-Zeile.

    Seit 07.08. wirkt die Historie AUCH auf den Zustand — aber ausschließlich als
    gedeckelter Dämpfungsterm im Last-Score (_wesen_memory, ±WESEN_MEM_CAP) und nur, wenn
    die Journaldatei da ist. Ohne sie rechnet das Wesen wie vorher rein aus dem heutigen
    Board; ein frischer Checkout verliert also Nuance, nie Funktion."""
    res = _wesen_core(board, today, archive, rituale, gate_silenced)
    if res["state"] != "hungry":
        res["why"] += _wesen_trend(res["state"], res.get("jetzt", 0), today)
    res.pop("jetzt", None)
    return res


# Reihenfolge = Eskalationsleiter der LAST-Achse. Die Ernährungs-Achse (fat/stuffed) und
# hungry stehen bewusst daneben, nicht drin.
WESEN_BANDS = ((WESEN_BAND_POCHEN, "pochen"), (WESEN_BAND_ACHE, "ache"),
               (WESEN_BAND_MUEDE, "muede"))


def _wesen_core(board: dict, today: date, archive: Path | None = None,
                rituale: list[dict] | None = None, gate_silenced: bool = False) -> dict:
    """Wesen-Zustand (Atari Stufe 2, T5; Schwellen kalibriert 21.07. nach Blatt).
    Deterministisch aus Board-Daten, Scope = To-dos-Board (Themen ohne Dev, ohne
    Cockpit-Pseudo-Items, ohne Personen). Vorrang: **hungry** > burst/smoke > stuffed > fat >
    pochen/ache/muede > healthy — der schlimmste Zustand gewinnt, das Wesen ist ein Alarm,
    kein Durchschnitt.

    hungry  (HUNGRIG): Heute-Zone Stufe 1 (21.07.) — mind. ein Ritual überfällig UND kein
            aktives Gate-Silence (Override/Snooze bereits eingerechnet in ritual_instance).
            Kommt VOR burst (Spec Stufe 1): das Ritual ist die dringendste Ansage.
    Last-Achse seit 07.08. GRADUIERT (s. WESEN_BAND_*): zwei stetige Beine (Menge, Alter)
    → ein Score `strain` → ein Band. Beide Beine sättigen WEICH (_soft_cap), oben gibt es
    also kein Plateau mehr. Dazu ein drittes, gedeckeltes RICHTUNGS-Bein aus der Historie
    (_wesen_direction): heute besser als der 7-Tage-Schnitt entlastet um bis zu zwei Bänder,
    heute schlechter kostet bis zu eines. Die Leiter von unten nach oben:
      muede   (MÜDE):           ab WESEN_BAND_MUEDE — leiseste Vorstufe
      ache    (KOPFSCHMERZEN):  ab WESEN_BAND_ACHE
      pochen  (KOPF POCHT):     ab WESEN_BAND_POCHEN — es wird eng, aber es reißt noch nicht
      burst   (KOPF PLATZT):    ab WESEN_BAND_TOP, und der produktive Abfluss trägt nicht mehr
      smoke   (KOPF RAUCHT):    dieselbe volle Last, aber ≥WESEN_RAUCHT_OUT produktive
                                Erledigungen in 7D und der Abfluss hält grob mit dem
                                Zufluss mit (WESEN_RAUCHT_RATIO) — 30.07.: „viel los,
                                aber auch viel gemacht". Maloche, kein Platzen.
    Ernährungs-Achse (Velocity, überarbeitet 22.07. — s. WESEN_VELO_GAP/_scope_of):
      stuffed (VOLLGESTOPFT):   produktiver Zufluss übersteigt den produktiven Abfluss um
                                ≥4 in 7 Tagen (ab ≥4 Neuen) — „es kommt mehr rein als raus"
      fat     (ÜBERFÜTTERT):    (a) Zuckerregel: ≥3 Board-/Dev-Items erledigt und damit mehr
                                als produktive — Spaßarbeit hat die echte verdrängt; oder
                                (b) ★-Item >3 Tage ohne Bewegung, während anderes lief
      healthy:                  Rest.
    Ernährung schlägt Last (fat/stuffed vor pochen/ache/muede): die Last steht ohnehin in
    jeder Warum-Zeile, die Ernährungs-Diagnose gäbe es sonst nie zu sehen. Jede Warum-Zeile
    trägt seit 22.07. den 7-Tage-Saldo RAUS/REIN (+ BOARD, wenn Zuckerwerk dabei war).

    Rückgabe trägt zusätzlich `strain` (0..~1.6), `menge`/`alter` (die einzelnen Beine) und
    `lead` (welches Bein führt). Das Frontend baut daraus Gesicht und Farbton stufenlos:
    Alter → Augen/Lider, Menge → Mund. 07.08.: „vllt gibt es auch verschiedene negativ
    parameter, und das ändert nur einen teil des wesens"."""
    if rituale and not gate_silenced:
        overdue = [r for r in rituale if r["status"] == "overdue"]
        if overdue:
            return {"state": "hungry", "why": "RITUAL OVERDUE: " + ", ".join(r["title"] for r in overdue)}
    archive = archive or BOARD_ARCHIVE
    iso7 = (today - timedelta(days=7)).isoformat()
    iso3 = (today - timedelta(days=3)).isoformat()
    themes = [t for t in board["themes"] if not _is_dev_theme(t["name"])]
    items = [it for t in themes for col in t["cols"].values() for it in col]
    jetzt = [it for t in themes for it in t["cols"].get("Jetzt", []) if not it["done"]]
    # Undatierte Items zählen wie „heute angelegt" (Verhalten unverändert seit 21.07.).
    ages = sorted(((today - date.fromisoformat(it.get("date") or today.isoformat())).days
                   for it in jetzt), reverse=True)
    oldest_days = ages[0] if ages else 0
    alt_count = sum(1 for a in ages if a > WESEN_ALTER)
    n = len(jetzt)
    menge_leg, alter_leg = _wesen_legs(n, alt_count, oldest_days)
    strain_today = _wesen_strain(menge_leg, alter_leg)
    load_today = _wesen_strain(*_wesen_legs(n, alt_count, oldest_days, soft=False))
    adjust, mittel = _wesen_direction(today, load_today)
    strain = max(0.0, strain_today - adjust)
    # Welches Bein führt? Nur für die Darstellung (Augen vs. Mund), nie für den Zustand.
    m_share, a_share = WESEN_W_MENGE * menge_leg, WESEN_W_ALTER * alter_leg
    lead = "menge" if m_share > a_share * 1.25 else "alter" if a_share > m_share * 1.25 else "beide"
    # `strain_raw` = die Last OHNE Richtungs-Korrektur. Genau die schreibt die Historie mit
    # (s. _wesen_snapshot) — sonst würde das Richtungs-Bein morgen seinen eigenen Effekt
    # von gestern als „Messwert" wiederfinden und sich selbst aufschaukeln.
    grad = {"strain": round(strain, 3), "strain_raw": round(strain_today, 3),
            "menge": round(menge_leg, 3), "alter": round(alter_leg, 3),
            "lead": lead, "jetzt": n, "dir": round(adjust, 3)}
    # Zufluss/Abfluss beide PRODUKTIV gezählt (22.07.): vorher war genau das asymmetrisch —
    # Zufluss ohne Dev, Abfluss MIT. Eine Woche Board-Basteln ließ das Wesen also gesund
    # aussehen, obwohl produktiv nichts rausging. Genau der Fall, den der Owner beschrieben hat.
    # Velocity hat einen WEITEREN Scope als die Last-Beine: Dev (Work) ist echte Arbeit
    # und zählt auf BEIDEN Seiten mit (nur Board/Tools sind Zuckerwerk) — sonst entstünde
    # dieselbe Asymmetrie nochmal, nur andersherum.
    velo_items = [it for t in board["themes"] if not _is_sugar_theme(t["name"])
                  for col in t["cols"].values() for it in col]
    inflow = sum(1 for it in velo_items if not it["done"] and (it.get("date") or "") >= iso7)
    # Abfluss ROLLEND über dieselben 7 Tage wie der Zufluss. Vorher stand hier
    # _done_this_week (Mo–heute): an einem Dienstag verglich die Regel 7 Tage Zufluss
    # gegen 2 Tage Abfluss und feuerte fast zwangsläufig. (Fix 21.07.)
    outflow = _done_since(board, iso7, archive, "prod")
    board_done = _done_since(board, iso7, archive, "board")
    vel = f"OUT {outflow} · IN {inflow} (7D)" + (f" · BOARD {board_done}" if board_done else "")
    # LAST als Prozentzahl steht vorn: sie ist die einzige Größe, die sich bei JEDEM
    # Häkchen bewegt — ohne sie bliebe der graduierte Score für den Owner unsichtbar.
    alt_txt = f"{alt_count}×>{WESEN_ALTER}D" if alt_count else f"MAX {oldest_days}D"
    # Die Richtung gehört SICHTBAR in die Warum-Zeile: ohne sie wäre eine Entlastung um
    # zwei Bänder eine unerklärte Zahl („warum ist das grün, es sind doch 22 Items?").
    dir_txt = ""
    if mittel is not None and abs(adjust) >= 0.005:
        dir_txt = (f" · {'↓ DOWN' if adjust > 0 else '↑ UP'} "
                   f"{round(abs(load_today - mittel) * 100)}% vs {WESEN_DIR_DAYS}D AVG")
    why_last = (f"LOAD {round(strain * 100)}% · NOW {n}/{WESEN_JETZT} · "
                f"AGE {alt_txt} · {vel}{dir_txt}")
    if strain >= WESEN_BAND_TOP:
        # „Es geht was raus" schlägt „es ist viel" — sonst steht das Wesen in jeder
        # arbeitsreichen Woche gleich laut auf PLATZT und der Alarm sagt nichts mehr.
        rauchend = outflow >= WESEN_RAUCHT_OUT and outflow >= inflow * WESEN_RAUCHT_RATIO
        return {**grad, "state": "smoke" if rauchend else "burst", "why": why_last}
    if inflow >= WESEN_VELO_MIN and inflow - outflow >= WESEN_VELO_GAP:
        return {**grad, "state": "stuffed", "why": f"{vel} → BALANCE −{inflow - outflow}"}
    if board_done >= WESEN_ZUCKER and board_done > outflow:
        return {**grad, "state": "fat",
                "why": f"SUGAR WORK: BOARD {board_done} · PRODUCTIVE {outflow} (7D)"}
    for schwelle, state in WESEN_BANDS:
        if strain >= schwelle:
            return {**grad, "state": state, "why": why_last}
    done_week = _done_this_week(board, today, archive, "prod")
    stale_star = [it for it in items if not it["done"] and it.get("mark")
                  and (it.get("date") or "") <= iso3]
    if stale_star and done_week > 0:
        return {**grad, "state": "fat",
                "why": f"★ STALE {max(0, (today - date.fromisoformat(stale_star[0]['date'])).days)}D · {done_week} OTHERS DONE"}
    moved_star = sum(1 for it in items if it.get("mark") and (it.get("date") or "") >= iso7)
    # dir_txt auch hier: kommt er gerade aus einem Alarm-Band heraus, ist genau DAS die
    # Erklärung dafür, dass das Wesen wieder gesund aussieht — sonst wirkt es willkürlich.
    why = (f"{vel}" + (f" · ★ ITEMS MOVING ({moved_star})" if moved_star else "") + dir_txt)
    return {**grad, "state": "healthy", "why": why}


# ── Superboard-Testrig ("I want this to be really easy") ─────────────────────
# Superboard is the shipped form of this board. To see it the way a stranger would
# after downloading it, the repo ships `scripts/testrig.sh`: build a wheel, install
# it into an empty workspace, serve it on its own port. The terminal path works —
# nobody takes it, though, because it is three steps and one of them (deleting the
# workspace first) can quietly go wrong: a rig on a USED workspace looks like a
# fresh install and isn't one.
#
# Hence this button. The rig mechanics stay entirely in the script next door
# (wheel, ports, delete-guard, identity); here only a process gets started and we
# report whether the port answers — no second place carries rig knowledge.
TESTRIG_PORT = int(os.environ.get("RIG_PORT", "").strip() or 47850)   # = default in testrig.sh
TESTRIG_LOG = Path(tempfile.gettempdir()) / "superboard-testrig.log"
TESTRIG_LOCK = threading.Lock()
TESTRIG_STATE: dict = {"proc": None, "error": ""}


def testrig_script() -> Path | None:
    """First in this repo (so the button works when running inside Superboard
    itself), else in the Superboard checkout next to it. SUPERBOARD_REPO overrides
    the default path — same convention as port_to_superboard.py."""
    for base in (GC_ROOT,
                 Path(os.environ.get("SUPERBOARD_REPO", "").strip()
                      or Path.home() / "Documents" / "superboard").expanduser()):
        script = base / "scripts" / "testrig.sh"
        if script.is_file():
            return script
    return None


def testrig_listening() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", TESTRIG_PORT)) == 0


def _testrig_log_tail(lines: int = 6) -> str:
    try:
        return " · ".join(TESTRIG_LOG.read_text(errors="replace").strip().splitlines()[-lines:])[:400]
    except OSError:
        return ""


def testrig_status() -> dict:
    script = testrig_script()
    with TESTRIG_LOCK:
        proc, error = TESTRIG_STATE["proc"], TESTRIG_STATE["error"]
    up = testrig_listening()
    return {"available": script is not None,
            "script": str(script) if script else "",
            "port": TESTRIG_PORT, "url": f"http://localhost:{TESTRIG_PORT}",
            "log": str(TESTRIG_LOG),
            "running": up,
            "starting": bool(proc is not None and proc.poll() is None and not up),
            "error": error}


def start_testrig(mode: str) -> tuple[int, dict]:
    """▶ of the testrig button. `fresh` = wipe the workspace, rebuild the wheel, serve
    (the first-install view) · `up` = start it if it is not already running · `stop`
    = shut it down.

    `fresh` stops the rig SYNCHRONOUSLY first, before the new run starts in the
    background. That way the port is guaranteed free once this request answers — and
    the waiting page can read "port answers" as "the NEW rig is up" without trickery,
    instead of jumping into the old state it was meant to replace."""
    if mode not in ("fresh", "up", "stop"):
        return 400, {"error": f"unknown mode {mode!r}"}
    script = testrig_script()
    if script is None:
        return 404, {"error": "scripts/testrig.sh not found — no Superboard checkout "
                              "here or at SUPERBOARD_REPO"}
    repo = script.parent.parent

    def run_sync(sub: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(script), sub], cwd=repo, capture_output=True,
                              text=True, timeout=90, stdin=subprocess.DEVNULL)

    if mode == "stop":
        done = run_sync("stop")
        with TESTRIG_LOCK:
            TESTRIG_STATE.update({"proc": None, "error": ""})
        if done.returncode != 0:
            return 409, {"error": (done.stderr or done.stdout or "stop failed").strip()[:300]}
        return 200, {"ok": True, "running": False}

    if mode == "up" and testrig_listening():
        return 200, {"ok": True, "running": True, "url": f"http://localhost:{TESTRIG_PORT}"}

    with TESTRIG_LOCK:
        cur = TESTRIG_STATE["proc"]
        if cur is not None and cur.poll() is None and not testrig_listening():
            return 409, {"error": "a rig is already starting"}

    if mode == "fresh" and testrig_listening():
        done = run_sync("stop")
        if done.returncode != 0:
            return 409, {"error": (done.stderr or done.stdout or "stop failed").strip()[:300]}

    try:
        log = TESTRIG_LOG.open("wb")          # every start begins its own log
    except OSError as e:
        return 500, {"error": f"cannot write {TESTRIG_LOG}: {e}"}
    proc = subprocess.Popen(["sh", str(script), mode], cwd=repo, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    with TESTRIG_LOCK:
        TESTRIG_STATE.update({"proc": proc, "error": ""})

    def watch() -> None:
        proc.wait()
        # A negative code is a signal — i.e. an intentional end (stop, restart,
        # Ctrl-C). Only a positive code is a real failure (uv missing, build
        # broken, port taken), and only that one deserves attention.
        if proc.returncode > 0 and not testrig_listening():
            with TESTRIG_LOCK:
                if TESTRIG_STATE["proc"] is proc:
                    TESTRIG_STATE["error"] = (f"testrig.sh {mode} exited with "
                                              f"{proc.returncode} · {_testrig_log_tail()}")

    threading.Thread(target=watch, daemon=True).start()
    return 202, {"ok": True, "starting": True, "log": str(TESTRIG_LOG),
                 "url": f"http://localhost:{TESTRIG_PORT}"}


# The waiting page. It is the actual trick behind "one click": the click itself MUST
# open the tab (afterwards the browser blocks popups), but the rig needs half a
# minute. So the click opens this page immediately, it kicks off the run, waits
# visibly, and replaces itself with the rig. A failure ends here with the last log
# lines instead of a blank tab.
TESTRIG_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Superboard test rig</title>
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#14161a;color:#e8e6e3;
      font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
 .card{max-width:34rem;padding:2rem 2.4rem;text-align:center}
 h1{font-size:1.25rem;margin:0 0 .4rem;font-weight:600}
 p{margin:.4rem 0;color:#a9a6a1}
 .dots::after{content:"";animation:d 1.4s steps(4,end) infinite}
 @keyframes d{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
 code{color:#8fb8ff;font-size:.85em;word-break:break-all}
 a{color:#8fb8ff}
 .err{color:#ff9d8a;text-align:left;white-space:pre-wrap;font-size:.85em;margin-top:1rem}
</style>
<div class=card>
  <h1 id=h>Building a fresh Superboard<span class=dots></span></h1>
  <p id=s>Building a wheel, installing it into an empty workspace, serving it — that
     takes a few seconds. This tab moves on by itself.</p>
  <p><code id=u></code></p>
  <div class=err id=e></div>
</div>
<script>
const MODE = new URLSearchParams(location.search).get("mode") === "up" ? "up" : "fresh";
const $ = (i) => document.getElementById(i);
const fail = (msg) => { $("h").textContent = "The rig did not come up";
  $("s").textContent = "Raw log: " + LOG; $("e").textContent = msg; };
let LOG = "";
(async () => {
  let r, j;
  try {
    r = await fetch("/api/testrig", {method:"POST", headers:{"Content-Type":"application/json"},
                                     body: JSON.stringify({mode: MODE})});
    j = await r.json();
  } catch (e) { return fail(String(e)); }
  $("u").textContent = j.url || "";
  LOG = j.log || "";
  if (!r.ok) return fail(j.error || ("HTTP " + r.status));
  if (j.running) return location.replace(j.url);
  if (MODE === "up") $("h").firstChild.textContent = "Starting the test rig";
  for (let i = 0; i < 240; i++) {                 // ~6 min cap; a build takes ~20-40 s
    await new Promise(res => setTimeout(res, 1500));
    let st;
    try { st = await (await fetch("/api/testrig", {cache:"no-store"})).json(); }
    catch { continue; }                            // board briefly gone? keep waiting
    if (st.running) return location.replace(st.url);
    if (st.error) return fail(st.error);
  }
  fail("Six minutes in, the port still does not answer.");
})();
</script>
"""


def run_cockpit_action(board_path: Path, key: str, base_url: str, claude_cmd: str,
                       model: str = "") -> tuple[int, dict]:
    """Quick Action ausführen (E3): find-or-create des Pseudo-Items in '# Cockpit',
    kurzer @gc:-Klick-Turn, dann der normale Agent-Run über die bestehende Maschinerie
    (persistenter Faden pro Action — Ergebnis lebt dort, R1-7). Die MISSION lebt
    im Item-Body (Marker action:<key> + Prompt aus actions.json, bei jedem Klick
    synchronisiert): Frisch-Runs bekommen den Body in den Prompt, Resume-Runs kennen
    die Mission aus ihrer Session — der Klick-Turn bleibt ein Einzeiler, board.md
    wächst pro Klick nur um eine Zeile. Auth-Bestätigung ist UI-Sache (Zwei-Klick).

    Gibt (HTTP-Code, Payload) zurück statt selbst zu antworten: eine reine Antwort-Funktion
    lässt sich leichter aus verschiedenen Kontexten aufrufen (aktuell: der
    `/api/action-run`-Handler)."""
    import gc_runner
    if model not in MODEL_CHOICES:
        return 400, {"error": "model must be one of: "
                              + ", ".join(m or "default" for m in MODEL_CHOICES)}
    actions, err = load_actions()
    action = next((a for a in actions if a["key"] == key), None)
    if action is None:
        return 404, {"error": f"Unknown action: {key or '(empty)'}" + (f" — {err}" if err else "")}
    marker = f"action:{key}"
    with board_write_guard(board_path):
        raw = board_path.read_text()
        board = parse_board(raw)
        if lost_total(raw, board) > 0:
            return 409, {"error": "The board has unparsed lines — action blocked"}
        item = next((it for it in board.get("cockpit", []) if marker in it.get("body", [])), None)
        with RUN_LOCK:
            if item and item.get("id") and (item["id"] in RUNNING or item["id"] in QUEUED):
                # VOR dem Turn-Append prüfen — sonst läge ein @gc: herum, das der
                # Auto-Retrigger nach dem laufenden Run gleich nochmal ausführt.
                return 409, {"error": "This action is already running"}
        if item is None:
            item = _new_item(False, action["label"])
            item["date"] = time.strftime("%Y-%m-%d")
            item["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
            board.setdefault("cockpit", []).append(item)
        if not item.get("id"):
            item["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
        # „Nur warnen, nie blocken" (17.08., Blatt auto-run-needs-input Q3=C): eine
        # offene Rückfrage der Vorrunde stoppt den neuen Run NICHT — aber sie darf nicht
        # verloren gehen („ich schaue ja nur das letzte an"). VOR dem Turn-Append gemessen,
        # weil der neue @gc:-Klick-Turn das Prädikat sonst sofort löscht; die Referenz
        # wandert als carryover in den Prompt (build_prompt), die UI warnt zusätzlich
        # vor dem ▶ (index.html needs-input). Gilt für ▶ UND Cron — beide landen hier.
        carryover = ""
        kind = item_needs_input(item)
        if kind:
            first = ""
            turns = [e for e in item.get("thread", []) if e.get("kind") != "sys"]
            if turns:
                txt = turns[-1].get("text", "")
                txt = sidecar.expand(txt) or txt
                first = txt.lstrip().split("\n", 1)[0][:200]
            carryover = {"sheet": f"decision sheet: {item_sheet(item)}",
                         "handoff": f"pending CLI handoff: {first}",
                         "frage": f"open question: {first}"}[kind]
        # Mission synchron halten: actions.json ist die Quelle der Wahrheit — jeder
        # ▶-Run startet frisch und liest sie damit neu.
        item["body"] = [marker, "···", action.get("prompt", "")]
        # ▶ = neue RUNDE, also frische Claude-Session (2026-08-06, Item 632bd6a8a6d5).
        # Der gespeicherte Resume-Pointer fliegt weg; gc_runner startet damit ohne --resume
        # und legt am Laufende die neue session_id ab. Der FADEN bleibt unangetastet und
        # geht bei frischen Runs ohnehin komplett als Text in den Prompt (build_prompt) —
        # „seit 3 Läufen offen"-Kontinuität überlebt, das teure Alt-Transkript nicht.
        # Weitertippen IM Faden resumt weiter wie bisher — der Schnitt hängt am ▶, nicht am Turn.
        _retire_session(item, item.get("session", ""), "")  # alte UUID vor dem Wipe in die Historie
        item["session"] = ""
        item["thread"].append({"kind": "ask", "text": f"▶ Run {action['label']}"})
        fd, tmp = tempfile.mkstemp(dir=board_path.parent, prefix=".board-")
        with os.fdopen(fd, "w") as f:
            f.write(serialize_board(board))
        os.replace(tmp, board_path)
        gc_id = item["id"]
        pending = pending_entry("cockpit", "Cockpit", None, item)
        if carryover:
            pending["carryover"] = carryover
    # Notbremse pro Action: optionales "timeout" in actions.json (Sekunden), sonst
    # DEFAULT_TIMEOUT. Der Default sind bereits 60 min Gesamtlaufzeit. Der Haken bei
    # langen Ritualen ist meist nicht diese Uhr, sondern IDLE_TIMEOUT (Stillstand ohne Ereignis).
    # Das Feld steht bereit, falls ein Ritual je darüber hinauswächst.
    try:
        run_timeout = int(action.get("timeout") or gc_runner.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        run_timeout = gc_runner.DEFAULT_TIMEOUT
    if not launch_gc_run(pending, base_url, claude_cmd, run_timeout, model=model):
        return 409, {"error": RESTART_DRAIN_MSG if restart_draining() else "This action is already running"}
    return 202, {"ok": True, "id": gc_id, "key": key}


# Tages-Snapshot der Wesen-Kennzahlen. Zweck (21.07.): die Schwellen sind bewusst
# ABSOLUT und geraten — in 2-3 Wochen soll man sie an echten Verlaufsdaten nachjustieren
# statt wieder zu schätzen (Board-Item „Wesen-Schwellen nachjustieren"). Bewusst KEIN
# eigener Cron: der erste /api/cockpit-Aufruf des Tages schreibt die Zeile, das genügt
# für Tages-Granularität und kann nicht vergessen werden.
WESEN_HISTORY = _p.JOURNAL / "wesen-history.jsonl"


def _wesen_snapshot(board: dict, today: date, wesen: dict) -> None:
    """Eine Zeile pro Tag nach journal/wesen-history.jsonl. Darf NIE den Endpoint
    mitreißen — jeder Fehler wird geschluckt (die Kachel ist wichtiger als die Statistik)."""
    try:
        iso = today.isoformat()
        if WESEN_HISTORY.is_file():
            tail = WESEN_HISTORY.read_text(encoding="utf-8").strip().rsplit("\n", 1)
            if tail and tail[-1].startswith(f'{{"date": "{iso}"'):
                return  # heute schon geschrieben
        # Spiegelt bewusst den Filter aus wesen_status() (s. o.) — die Historie muss
        # dieselbe Grundgesamtheit zählen wie die Kachel, sonst vergleicht man Äpfel
        # mit Birnen beim Nachjustieren der Schwellen.
        themes = [t for t in board["themes"] if not _is_dev_theme(t["name"])]
        items = [it for t in themes for col in t["cols"].values() for it in col]
        jetzt = [it for t in themes for it in t["cols"].get("Jetzt", []) if not it["done"]]
        ages = sorted(((today - date.fromisoformat(it["date"])).days
                       for it in jetzt if it.get("date")), reverse=True)
        iso7 = (today - timedelta(days=7)).isoformat()
        row = {"date": iso, "state": wesen["state"], "jetzt": len(jetzt),
               "oldest_days": ages[0] if ages else 0,
               "jetzt_over_7d": sum(1 for a in ages if a >= 7),
               # Seit 07.08.: der graduierte Last-Score und das Alters-Bein exakt so, wie
               # _wesen_core sie gerechnet hat — aber die ROHLAST (ohne Richtungs-Korrektur),
               # sonst frisst das Richtungs-Bein morgen seinen eigenen Effekt von heute.
               "strain": wesen.get("strain_raw", wesen.get("strain")),
               "jetzt_over_14d": sum(1 for a in ages if a > WESEN_ALTER),
               "open_total": sum(1 for it in items if not it["done"]),
               "inflow_7d": sum(1 for it in items
                                if not it["done"] and (it.get("date") or "") >= iso7),
               # Abfluss seit 22.07. dreigeteilt mitgeschrieben — beim Nachjustieren der
               # Schwellen will man sehen, wie viel davon Zuckerwerk war (Board-Items
               # zählen nicht positiv). outflow_7d bleibt die Gesamtzahl (Zeitreihe stabil).
               "outflow_7d": _done_since(board, iso7, BOARD_ARCHIVE),
               "outflow_prod_7d": _done_since(board, iso7, BOARD_ARCHIVE, "prod"),
               "outflow_board_7d": _done_since(board, iso7, BOARD_ARCHIVE, "board"),
               "done_week": _done_this_week(board, today, BOARD_ARCHIVE)}
        WESEN_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with WESEN_HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 — Statistik darf den Endpoint nie 500en
        print(f"todo-board: wesen-snapshot: {e}", file=sys.stderr)


# Der laufende Prozess hält Server- UND Runner-Code als Modul-Objekte im Speicher —
# gc_runner.PROMPT_CONTRACT, AGENT_SETTINGS, die Timeouts: alles friert beim Start ein.
# Änderungen greifen erst nach einem Neustart, und genau das ist zweimal unbemerkt
# schiefgegangen (2026-07-14 die verlorene personal/-Deny-Regel; 2026-08-07 ein 23 h alter
# Kontrakt — jeder Board-Run dieser 23 h bekam Anweisungen, die auf Platte längst korrigiert
# waren). Deshalb vergleicht das Cockpit jetzt die mtime der beiden Dateien, die den Run-Pfad
# bestimmen, mit dem Prozessstart. Bewusst NUR diese zwei: index.html liest der Server bei
# jedem Request frisch (ein Reload im Browser reicht), board.md sind Daten.
STALE_WATCH = ("server.py", "gc_runner.py")


def server_stale() -> list[str]:
    """Dateien, die seit dem Serverstart geändert wurden — leer heißt: der Prozess ist aktuell."""
    out = []
    for name in STALE_WATCH:
        try:
            if (ROOT / name).stat().st_mtime > SERVER_START:
                out.append(name)
        except OSError:  # Datei weg/unlesbar ist kein Grund, das Cockpit zu 500en
            pass
    return out


INTEGRITY_CACHE: dict = {"at": 0.0, "data": None}
INTEGRITY_TTL = 60          # s — Minuten statt Stunden ist das Ziel, Sekunden waere Verschwendung


def integrity_status(force: bool = False) -> dict:
    """Verlust-Waechter fuer die Board-Kopfzeile (2026-08-25).

    Ein Item verschwand einmal still aus board.md, ueberschrieben von einem parallelen
    Write. Gefunden wurde es nur, weil jemand den NAECHTLICHEN Guard zufaellig von Hand
    laufen liess - regulaer waere es erst am naechsten Morgen aufgefallen. Also: derselbe
    Waechter, nur hier und jetzt, und nur die VERLUST-Klasse (`loss_issues`) - abgehakte
    Items ohne Datum bleiben naechtlich, sonst leuchtete der Kopf bei jedem frisch
    gesetzten Haken kurz rot, ohne dass etwas kaputt waere.

    Gecacht (INTEGRITY_TTL), weil der Check `inbox/gc-threads/` durchglobt und /api/cockpit
    im Cockpit-View sekuendlich gepollt wird. Liefert IMMER ein Dict - ein Fehler im
    Waechter darf das Cockpit nie mitreissen."""
    now = time.time()
    if not force and INTEGRITY_CACHE["data"] is not None and (now - INTEGRITY_CACHE["at"]) < INTEGRITY_TTL:
        return INTEGRITY_CACHE["data"]
    try:
        import board_integrity
        out = {"ok": True, "issues": board_integrity.loss_issues()}
    except Exception as e:  # noqa: BLE001 — der Kopf darf nie 500en
        out = {"ok": False, "issues": [], "error": str(e)[:200]}
    INTEGRITY_CACHE.update(at=now, data=out)
    return out


def cockpit_payload(board: dict, today: date | None = None) -> dict:
    """One read-only payload for attention, triage, actions, and board state."""
    today = today or date.today()
    with RUN_LOCK:
        live = {"running": len(RUNNING), "queued": len(QUEUED)}
    now = ritual_now()
    wesen = wesen_status(board, today, rituale=rituale_status(now), gate_silenced=gate_silence_active(now))
    _wesen_snapshot(board, today, wesen)
    return {"today": today.isoformat(),
            "kpis": board_kpis(board, today) | live,
            "attention": attention_hints(board, today),
            "triage": triage_status(),
            # Atari-Kopfzeile + Wesen (Stufe 2): Hi-Score = erledigt seit Montag,
            # Wesen-Zustand deterministisch aus Board-Daten (s. wesen_status).
            "done_week": _done_this_week(board, today, BOARD_ARCHIVE),
            # view-accurate (To-dos tab / Dev tab) — the header shows the value of the
            # active view; done_week stays board-wide for outside readers.
            "done_week_view": _done_week_views(board, today, BOARD_ARCHIVE),
            "wesen": wesen,
            "server_started": SERVER_START,
            "server_stale": server_stale(),
            # Verlust-Waechter fuer die Kopfzeile — siehe integrity_status().
            "integrity": integrity_status()}


class Handler(BaseHTTPRequestHandler):
    board_path: Path = DEFAULT_BOARD

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _repo_file(self, rel: str) -> Path | None:
        """Repo-relativen oder absoluten lokalen Pfad sicher auflösen — oder None.

        Der Board-Server lauscht nur auf 127.0.0.1, aber das ist kein Freibrief: hier geht ein
        Fenster ins Repo auf, also gilt Default-Deny. Erlaubt ist NUR, was ein Faden-Turn
        sinnvollerweise verlinkt (Doku/Notizen/Code), und nur unterhalb der Repo-Wurzel.
        Geblockt: versteckte Config-Verzeichnisse, jede .env*, alles unter einem Dot-Verzeichnis (.git!),
        Symlinks aus dem Repo heraus, alles unter personal/private, und alles über 2 MB.
        """
        if not rel or "\\" in rel:
            return None
        raw = Path(rel)
        p = raw.resolve() if raw.is_absolute() else (GC_ROOT / raw).resolve()
        # Absolute links are just a more convenient spelling of the same repo window.
        # Relativise FIRST, THEN apply the same dot-/top-level rules — so an absolute
        # path under the repo root opens, but never `/tmp/x.md` or a dotfile.
        if not p.is_relative_to(GC_ROOT):
            return None
        parts = p.relative_to(GC_ROOT).parts
        if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            return None
        if parts[0] in BLOCKED_TOP_DIRS:
            return None
        if p.suffix.lower() not in READABLE_SUFFIXES:
            return None
        # .resolve() folgt Symlinks — der Vergleich oben fängt Ausbrüche aus dem Repo ab.
        if not p.is_file():
            return None
        if p.stat().st_size > 2_000_000:
            return None
        return p

    # ── SSE: der erste long-lived Request im Board ────────────────────────────
    # _send() oben setzt IMMER Content-Length und schreibt den Body am Stück — für
    # einen Strom, der offen bleibt, braucht es diesen eigenen Schreibweg daran
    # vorbei: Status + Header von Hand, danach Häppchen direkt auf self.wfile.
    # protocol_version ist HTTP/1.0, „Verbindung endet mit close" ist also eh der
    # Vertrag; das explizite Connection: close dokumentiert nur, dass hier bewusst
    # KEIN Content-Length kommt.
    def _sse_head(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # falls je ein Proxy davor puffert
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse_write(self, chunk: bytes) -> bool:
        """False = Gegenseite weg (BrokenPipe/ConnectionReset) — der Aufrufer beendet
        dann seine Schleife, statt den Thread am toten Socket leaken zu lassen."""
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _sse_stream(self, gid: str) -> None:
        """GET /api/gc-stream-sse — pusht den Ereignisstrom eines Runs, statt ihn vom
        Browser alle 5 s holen zu lassen. Quelle und Zeilenformat wie stream_view();
        getailt wird aber INKREMENTELL über gc_runner.StreamTail (Byte-Offset plus
        Halbzeilen-Puffer), nicht per Neu-Lesen der Datei bei jedem Push.

        Das Event-`id:` ist der Byte-Offset hinter der letzten VOLLSTÄNDIGEN Zeile —
        der Browser schickt ihn beim Reconnect als Last-Event-ID zurück, und der Tail
        setzt genau dort wieder auf: nichts kommt doppelt, nichts fehlt. Endet der
        Run (Schluss-Event im Strom oder Registry sagt „läuft nicht mehr"), geht ein
        `event: end` raus und die Verbindung schließt; das Frontend holt sich den
        autoritativen Schlussstand dann über den alten Endpoint."""
        import gc_runner  # lazy — Stil wie an den anderen Aufrufstellen

        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return -1.0

        self._sse_head()
        hits = sorted(JOURNAL_DIR.glob(f"run-{gid}-*.out.json"), key=_mtime)
        if not hits:
            with RUN_LOCK:
                live = gid in RUNNING
            if live:
                # RUNNING is registered just before gc_runner creates the journal.
                # A click in that narrow gap used to close SSE immediately and fall
                # back to the previous killed stream. Acknowledge this connection,
                # then wait briefly for the real current file instead.
                initial = json.dumps({"rows": [], "size_mb": 0.0, "profile": "",
                                      "waiting": True}, ensure_ascii=False)
                if not self._sse_write(f"data: {initial}\n\n".encode()):
                    return
                deadline = time.time() + 15
                while time.time() < deadline and not hits:
                    time.sleep(0.05)
                    hits = sorted(JOURNAL_DIR.glob(f"run-{gid}-*.out.json"), key=_mtime)
                    with RUN_LOCK:
                        live = gid in RUNNING
                    if not live:
                        break
            if not hits:
                # Kein Strom da (Run erfolgreich abgeräumt, oder nur noch unter killed/) —
                # sofort schließen; das Frontend fällt auf den Polling-Pfad zurück, der
                # auch die aufgehobenen Ströme gekillter Runs kennt.
                self._sse_write(b"event: end\ndata: {}\n\n")
                return
        src = hits[-1]
        tail = gc_runner.StreamTail(src)
        pending: list[dict] = []

        def _collect(ev: dict) -> None:
            row = _stream_row(ev)
            if row is None:
                return
            pending.extend(row["rows"]) if row.get("kind") == "multi" else pending.append(row)

        tail.on_event = _collect
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        meta_path = src.with_name(src.name.removesuffix(".out.json") + ".meta.json")
        try:
            profile = str(json.loads(meta_path.read_text()).get("model") or "")
        except (OSError, ValueError, TypeError):
            profile = ""
        lei = str(self.headers.get("Last-Event-ID", "") or "")
        if lei.isdigit():
            tail.offset = min(int(lei), size)
        elif size > STREAM_TAIL_BYTES:
            # Frisch geöffnet auf einem dicken Strom: wie stream_view nur den Schwanz.
            # Binär bis zum nächsten Zeilenende vorspulen — StreamTail liest strict
            # utf-8, ein Einstieg mitten im Mehrbyte-Zeichen würde ihn werfen.
            try:
                with open(src, "rb") as f:
                    f.seek(size - STREAM_TAIL_BYTES)
                    f.readline()
                    tail.offset = f.tell()
            except OSError:
                pass
        start = last_write = time.time()
        # A connected but still-empty stream used to leave the UI on the content-free
        # word "loading…". A provider may spend tens of seconds before its first
        # event, so acknowledge the live connection immediately and name the actual
        # run profile from the journal meta. No event id: this status does not
        # consume a byte and is safe across reconnects.
        if size == 0:
            initial = json.dumps({"rows": [], "size_mb": 0.0, "profile": profile,
                                  "waiting": True}, ensure_ascii=False)
            if not self._sse_write(f"data: {initial}\n\n".encode()):
                return
            last_write = time.time()
        while True:
            try:
                tail.poll()
            except (UnicodeDecodeError, ValueError):
                return  # kaputter Einstiegspunkt — zumachen, der Browser verbindet neu
            if pending:
                # Offset nur bis zur letzten KOMPLETTEN Zeile melden — die halbe Zeile
                # im Puffer würde ein Reconnect sonst verschlucken.
                committed = max(0, tail.offset - len(tail._buf.encode("utf-8")))
                try:
                    size_mb = round(src.stat().st_size / 1e6, 1)
                except OSError:
                    size_mb = 0.0
                data = json.dumps({"rows": pending, "size_mb": size_mb,
                                   "profile": profile, "waiting": False}, ensure_ascii=False)
                pending.clear()
                if not self._sse_write(f"id: {committed}\ndata: {data}\n\n".encode()):
                    return
                last_write = time.time()
            # Ende sagt AUSSCHLIESSLICH die Registry, nie ein `done` im Strom.
            # Gelernt am lebenden Objekt (12.08.): eine Journal-Datei sammelt mehrere
            # Turns derselben Session, ein abgeschlossener Vorgänger-Turn liegt also
            # als `done` mitten drin. Wer darauf schließt, macht direkt nach dem
            # Aufholen zu — die Verbindung hielt 0 s, das Panel fiel still aufs
            # Polling zurück und alles sah aus wie „funktioniert".
            with RUN_LOCK:
                live = gid in RUNNING
            if not live:
                self._sse_write(b"event: end\ndata: {}\n\n")
                return
            if time.time() - start > SSE_MAX_CONN_S:
                return  # Kappe: kommentarlos schließen → Reconnect mit Last-Event-ID
            if time.time() - last_write >= SSE_HEARTBEAT_S:
                if not self._sse_write(b":\n\n"):
                    return
                last_write = time.time()
            time.sleep(SSE_POLL_S)

    # ── Cross-origin write guard (security fix) ────────────────────────────────
    # The server binds to 127.0.0.1 only, but "only the local user can reach it"
    # is not the same as "only the local user can WRITE to it": any website open
    # in a browser tab can still fire a "simple" cross-origin POST — one exempt
    # from a CORS preflight — straight at localhost. Verified reproduction: a
    # POST with `Origin: https://attacker.example` and `Content-Type: text/plain`
    # was accepted, created a card, and started a real agent run. Two independent
    # checks below, either one blocks it; neither slows down a local caller that
    # never sends an Origin header at all (curl or any non-browser client).
    def _is_own_origin(self, origin: str) -> bool:
        """True if `origin` names this same server — same localhost spelling,
        same bound port. Scheme is deliberately not part of the check: this
        server only ever speaks plain HTTP, so matching host+port is precise
        enough without adding a second thing that has to line up."""
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        if (parts.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            return False
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return port == self.server.server_address[1]

    def _csrf_guard(self, length: int) -> str | None:
        """None = request is fine to process; otherwise the 403 message to send.

        - `Sec-Fetch-Site: cross-site` is a browser-set, unspoofable-by-the-page
          signal — trust it outright when present.
        - An `Origin` header that doesn't name this server is the CSRF case from
          the finding above; reject it. No `Origin` header at all is how every
          local tool looks (they're not browsers), so that passes.
        - Any request with a body must claim `Content-Type: application/json`.
          That alone removes the no-preflight path: text/plain,
          multipart/form-data and x-www-form-urlencoded are exactly the content
          types a cross-origin `fetch()`/`<form>` can send WITHOUT a preflight —
          application/json cannot leave a browser cross-origin without one.
        """
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return "cross-site request blocked"
        origin = (self.headers.get("Origin") or "").strip()
        if origin and not self._is_own_origin(origin):
            return "cross-origin request blocked"
        if length:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return "write requests must use Content-Type: application/json"
        return None

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, read_index_html(), "text/html; charset=utf-8")
        # Three static, self-contained onboarding pages. `/onboarding-showcase` is the
        # walkthrough ("Find your way around") and keeps its historic path plus the
        # `#threads`/`#off-duty` anchors that seeded cards deep-link to; `/welcome` is
        # the one-minute introduction card 1 opens, `/inspiration` the optional
        # "Get more from Superboard" page. All three are packaged fictional data.
        elif self.path in ("/welcome", "/welcome.html"):
            self._send(200, (ROOT / "welcome.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif self.path in ("/onboarding-showcase", "/onboarding-showcase.html"):
            self._send(200, (ROOT / "onboarding-showcase.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif self.path in ("/inspiration", "/inspiration.html"):
            self._send(200, (ROOT / "inspiration.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif self.path == "/testrig" or self.path.startswith("/testrig?"):
            self._send(200, TESTRIG_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/testrig":
            self._json(200, testrig_status())
        elif self.path == "/api/board":
            text = self.board_path.read_text()
            board = parse_board(text)
            lost = lost_total(text, board)   # VOR annotate_sheets: die Guards zählen gegen den Rohtext
            annotate_sheets(board)
            annotate_turn_times(board)
            annotate_cross_run_cache(board)
            with RUN_LOCK:
                running, since, queued = sorted(RUNNING), dict(RUNNING), sorted(QUEUED)
                compacting, beats = sorted(COMPACTING), _public_beats()
            # `instance` = the folder name (GC_ROOT.name), NOT configured — it derives
            # from the folder name so a checkout is self-identifying without setup. The
            # header shows it small as a path handle next to the brand.
            self._json(200, {"board": board, "etag": text_etag(text),
                             "lost": lost, "version": current_version(),
                             "instance": GC_ROOT.name,
                             "night_pause_enabled": _cfg.NIGHT_PAUSE_ENABLED,
                             "off_duty_hidden_topics": _cfg.OFF_DUTY_HIDDEN_TOPICS,
                             "off_duty_visible_topics": _cfg.OFF_DUTY_VISIBLE_TOPICS,
                             "running": running, "running_since": since, "queued": queued,
                             "compacting": compacting, "beats": beats,
                             "finished_recent": finished_recent(),
                             "killed_today": killed_today()})
        elif self.path.startswith("/api/docs/"):
            name = self.path[len("/api/docs/"):].split("?", 1)[0]
            text = read_product_doc(name)
            if text is None:
                return self._json(404, {"error": f"unknown doc: {name}",
                                        "available": sorted(DOC_SOURCES)})
            self._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8")
        elif self.path == "/api/runner-status":
            # Break the terminal-only preflight out to the UI: a newcomer without
            # Claude Code installed must learn that from the board, not from a
            # scrollback line they never saw.
            state, message = runner_status()
            self._json(200, {"state": state, "message": message})
        elif self.path == "/api/gc-pending":
            text = self.board_path.read_text()
            board = parse_board(text)
            pending = [pending_entry(s, n, c, it, board) for s, n, c, it in _all_items(board)
                       if thread_status(it) == "for_gc"]
            self._json(200, {"pending": pending, "etag": text_etag(text)})
        elif self.path == "/api/etag":
            with RUN_LOCK:
                running, queued, compacting = sorted(RUNNING), sorted(QUEUED), sorted(COMPACTING)
                since, beats = dict(RUNNING), _public_beats()
            # running_since + beats gehören hierher, weil die UI im Sekundentakt /api/etag
            # pollt, /api/board aber nur bei echter Änderung — ohne das stünde die
            # Laufzeit-/Fortschrittsanzeige minutenlang still.
            self._json(200, {"etag": file_etag(self.board_path), "running": running,
                             "queued": queued, "compacting": compacting,
                             "running_since": since, "beats": beats,
                             "finished_recent": finished_recent(),
                             "killed_today": killed_today()})
        elif self.path.startswith("/api/netcheck"):
            # Getriggert von der UI, sobald ein Run die 20-min-Schwelle reißt (s. netcheck()).
            self._json(200, netcheck(force=self.path.endswith("force=1")))
        elif self.path.startswith("/api/gc-prompt"):
            # Observability (2026-07-22): zeigt den Prompt, der zuletzt für dieses
            # Item an den Agenten ging — Kontrakt, Handoff-Hinweis, Faden-Ausschnitt,
            # Arbeitsstand. Bis hierhin war das die einzige echte Blackbox der Mechanik.
            # Geschrieben von gc_runner.RunJournal.save_prompt (journal/prompts/, je
            # Item die letzten 3). Kein Prompt da = Run älter als das Feature.
            gid = self.path.partition("?id=")[2].split("&")[0]
            if not re.fullmatch(r"[0-9a-f]{6,32}", gid):
                return self._json(400, {"error": "bad id"})
            import gc_runner  # lazy — Stil wie an den anderen Aufrufstellen

            d = JOURNAL_DIR / "prompts"
            # Sortierung bewusst aus gc_runner (mtime, nicht Dateiname) — der Name
            # endet auf einem Zufallssuffix und ordnet gleichsekuendige Runs falsch.
            hits = gc_runner.prompt_files(d, gid)
            if not hits:
                return self._json(404, {"error": "No prompt recorded (does the run predate this feature?)"})
            p = hits[-1]
            self._json(200, {"text": p.read_text(), "name": p.name,
                             "ts": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                             "count": len(hits)})
        elif self.path.startswith("/api/gc-stream-sse"):
            # Phase 3 (2026-08-10): Push statt Polling — bewusst NEBEN dem alten
            # /api/gc-stream, der als Fallback und für den Schlussstand bleibt.
            # WICHTIG: dieser Zweig muss VOR /api/gc-stream stehen — dessen
            # startswith() würde diesen Pfad sonst mit abfangen.
            gid = self.path.partition("?id=")[2].split("&")[0]
            if not re.fullmatch(r"[0-9a-f]{6,32}", gid):
                return self._json(400, {"error": "bad id"})
            self._sse_stream(gid)
        elif self.path.startswith("/api/gc-stream"):
            # 2026-07-27: „vllt kann man ja dort die json einsehen? also immer nur von
            # der letzten session, dann sehe ich wo man gerade ist. dann kann ich auch
            # abbrechen entscheiden." Genau dafür: der Ereignisstrom des LAUFENDEN Runs
            # (oder, wenn keiner läuft, der aufgehobene Strom des zuletzt gekillten).
            gid = self.path.partition("?id=")[2].split("&")[0]
            if not re.fullmatch(r"[0-9a-f]{6,32}", gid):
                return self._json(400, {"error": "bad id"})
            with RUN_LOCK:
                live = gid in RUNNING
            self._json(200, stream_view(JOURNAL_DIR, gid, live))
        elif self.path.startswith("/api/gc-receipt"):
            # Gegenstück zu /api/gc-prompt (2026-07-22, Blatt Q4=A): der Prompt zeigt,
            # was in den Run GING — das Receipt, was dabei HERAUSKAM (Commits, geänderte
            # Dateien, geblockte Aktionen, Todesursache). Geschrieben von receipt.write()
            # im Runner, nicht vom Agenten. Kein Receipt = Run älter als das Feature.
            gid = self.path.partition("?id=")[2].split("&")[0]
            if not re.fullmatch(r"[0-9a-f]{6,32}", gid):
                return self._json(400, {"error": "bad id"})
            hits = _receipt.files(gid)
            if not hits:
                return self._json(404, {"error": "No receipt recorded (does the run predate this feature?)"})
            p = hits[-1]
            self._json(200, {"text": p.read_text(), "name": p.name,
                             "ts": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                             "count": len(hits)})
        elif self.path.startswith("/gc-threads/"):
            # Sidecar-Dateien (lange Agent-Antworten) im Browser lesbar machen.
            # Nur sichere Dateinamen — kein Pfad-Traversal, nur .md aus inbox/gc-threads/.
            name = self.path[len("/gc-threads/"):]
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.md", name) or ".." in name:
                return self._json(404, {"error": "not found"})
            p = self.board_path.parent / "gc-threads" / name
            if not p.is_file():
                # Faden-Retention (sweep.py, 2026-07-21): Sidecars archivierter Items
                # wandern nach gc-threads/archive/ — alte Verweise sollen trotzdem
                # noch auflösen, statt 404 zu zeigen.
                p = self.board_path.parent / "gc-threads" / "archive" / name
                if not p.is_file():
                    return self._json(404, {"error": "not found"})
            self._send(200, p.read_bytes(), "text/plain; charset=utf-8")
        elif self.path.startswith("/repo-file/"):
            # Jeder Datei-Pfad, den ein Faden-Turn erwähnt, ist im Board klickbar (2026-07-14).
            # Read-only-Fenster ins Repo — deshalb eng geschnürt, siehe _repo_file().
            p = self._repo_file(unquote(self.path[len("/repo-file/"):]))
            if p is None:
                return self._json(404, {"error": "not found"})
            # .html gerendert (Entscheidungsblätter aus tmp/ sind genau dafür da), Rest als Text.
            ctype = "text/html" if p.suffix.lower() == ".html" else "text/plain"
            self._send(200, p.read_bytes(), f"{ctype}; charset=utf-8")
        elif self.path == "/api/actions":
            actions, err = load_actions()
            # Prompts bleiben server-seitig — die UI braucht nur Anzeige-Metadaten.
            # "status": frei textbarer Stand-Vermerk, den der Action-Agent am Ende seines
            # Laufs selbst in actions.json zurückschreibt (z.B. "Stand 2026-07-27: 0 offen").
            # Damit trägt die Karte einen Rückstands-Zähler, ohne dass der Server die Domäne
            # der Action kennen muss [27.07.: "wie viele noch offen sind, beim button"].
            # "status" nur mitsenden, wenn gesetzt — Actions ohne Vermerk behalten exakt
            # die alte Payload-Form (und die UI rendert dann keine leere Zeile).
            # "group": optionale Zugehörigkeit zu einer eigenen Cockpit-Zone (bisher nur
            # "wissens-kette"). Actions ohne das Feld landen wie bisher im Quick-Actions-Grid
            # [28.07., Blatt Q5=A: "nur die Kette raus, Rest bleibt wie er ist"].
            # "rhythm": frei getippter Soll-Rhythmus ("ideal: wöchentlich"). BEWUSST reiner
            # Text und keine gerechnete Fälligkeit [28.07., Blatt Q2=B: "das kann ich jetzt
            # am Anfang selber machen … die einfachste Variante"]. Getrennt von "status", weil
            # den der Action-Agent am Laufende überschreibt — der Rhythmus gehört dem Owner.
            # "schedule": diese Action wird von launchd/Cron von selbst gestartet; der Text
            # nennt die IST-Slots ("auto 8:20 + 13:20"). Getrennt von "rhythm", weil das der
            # SOLL-Wunsch ist — hier steht, was ohne einen Klick des Owners tatsächlich passiert
            # [16.08.: "ein visual marker, dass das zweimal täglich läuft"]. Reiner Text,
            # kein Parsen der plists: die Wahrheit steht in ~/Library/LaunchAgents, die Karte
            # zitiert sie nur — deshalb bei Fahrplan-Änderungen hier mitziehen.
            # "run_endpoint": eigener Startpfad statt /api/action-run — für Actions, die von
            # einem eigenen Trigger-Thread statt vom HTTP-Handler gestartet werden. MUSS
            # mitgesendet werden, sonst fällt die UI still auf /api/action-run zurück und
            # der Sonderpfad bleibt tot. Nur /api/-Pfade durchlassen:
            # actions.json ist lokal und vertrauenswürdig, aber der Wert landet ungeprüft
            # in einem fetch() der UI — eine externe URL wäre eine Exfiltrations-Kante.
            self._json(200, {"actions": [{**{k: a.get(k, "") for k in ("key", "label", "icon", "auth")},
                                          **({"status": a["status"]} if a.get("status") else {}),
                                          **({"group": a["group"]} if a.get("group") else {}),
                                          **({"rhythm": a["rhythm"]} if a.get("rhythm") else {}),
                                          **({"schedule": a["schedule"]} if a.get("schedule") else {}),
                                          **({"run_endpoint": a["run_endpoint"]}
                                             if str(a.get("run_endpoint", "")).startswith("/api/") else {})}
                                         for a in actions],
                             **({"error": err} if err else {})})
        elif self.path == "/api/board-lint":
            # WELCHE Zeilen würde ein Save vernichten — die Frage, die `lost` in
            # /api/board nur zählt. Read-only, fasst board.md nie an. Das Modul bekommt
            # dieses hier bereits geladene Server-Modul mit, statt server.py ein zweites
            # Mal zu importieren (2900 Zeilen pro Abruf, und ein zweiter Modulzustand).
            self._json(200, board_lint.lint(self.board_path.read_text(),
                                            server=sys.modules[__name__]))
        elif self.path == "/api/cockpit":
            # Cockpit-Zonen Kennzahlen (E2) + Attention (E6). Read-only, fasst board.md nie an.
            board = parse_board(self.board_path.read_text())
            self._json(200, cockpit_payload(board))
        elif self.path == "/api/rituale":
            # Heute-Zone Stufe 1: heutige Ritual-Instanzen + globales Gate-Silence.
            # Read-only — rituale.json + journal/rituale.jsonl, fasst board.md nie an.
            now = ritual_now()
            config, errors = _registries.load_rituals(RITUALE_FILE)
            self._json(200, {"rituale": rituale_status(now, config),
                             "gate_silenced": gate_silence_active(now),
                             **({"error": " · ".join(errors)} if errors else {})})
        elif self.path == "/api/dev-radar":
            # Live-Status der Dev-Items (MRs/PRs via glab/gh) — siehe dev_radar.py.
            # Read-only: der Radar fasst board.md nicht an, er liest nur. Fehler werden als
            # {"error": ...} durchgereicht statt zu werfen — der Button darf nie das Board killen.
            self._json(200, run_dev_radar())
        elif self.path in ("/apple-touch-icon.png", "/icon.png") and (ROOT / "icon.png").exists():
            # Beide Pfade auf dieselbe Datei: Dock/Homescreen holt apple-touch-icon,
            # der Browser-Tab das Favicon — beides ist seit 21.07. die GC-Wortmarke.
            self._send(200, (ROOT / "icon.png").read_bytes(), "image/png")
        else:
            self._json(404, {"error": "not found"})

    def _atomic_write(self, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.board_path.parent, prefix=".board-")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, self.board_path)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        rejection = self._csrf_guard(length)
        if rejection:
            if length:
                self.rfile.read(length)  # drain — the client already wrote the body
            return self._json(403, {"error": rejection})
        payload = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/api/board":
            with board_write_guard(self.board_path):
                raw = self.board_path.read_text()
                if payload.get("baseEtag") != text_etag(raw):
                    return self._json(409, {"error": "conflict", "etag": text_etag(raw)})
                disk = parse_board(raw)
                if lost_total(raw, disk) > 0:
                    # On-Disk-Zustand hat ungeparste Zeilen — Overwrite würde sie vernichten.
                    # (UI blockt das via `locked` selbst; das hier fängt direkte API-Clients.)
                    return self._json(409, {"error": "The board has unparsed lines — save blocked",
                                            "etag": text_etag(raw)})
                if "staging" not in payload["board"]:
                    # Gleiche Falle wie bei Cockpit: ein Tab mit altem JS kennt den
                    # Staging-Key nicht und wuerde die Sektion beim Whole-Board-Save
                    # still vernichten — samt der Vorschlaege, die noch niemand gesichtet hat.
                    payload["board"]["staging"] = disk.get("staging", [])
                if "cockpit" not in payload["board"]:
                    # "# Cockpit" ist server-owned (Quick-Action-Pseudo-Items, E3): ein
                    # Client mit altem JS kennt den Key nicht — sein Whole-Board-Save
                    # dürfte die Sektion samt Aktions-Fäden sonst still vernichten.
                    payload["board"]["cockpit"] = disk.get("cockpit", [])
                _migrate_legacy_gc(payload["board"])  # alte UI schickt gc[] statt thread[] → retten
                # Stale-Client-Guard (2026-08-25): ein Tab, der das Board VOR einem externen
                # Insert geladen hat, kennt neue Items nicht — sein Whole-Board-Save (auch der
                # einmalige 409-Retry mit frischem etag) wuerde sie still vernichten. Ein Item
                # darf nur verschwinden, wenn der Client das ausdruecklich sagt (`removedIds`,
                # gesetzt vom ✕ im UI) — alles andere ist kein Loeschen, sondern Nichtwissen
                # → 409, der Client laedt neu.
                removed = set(payload.get("removedIds") or [])
                disk_ids = {it["id"] for _s, _n, _c, it in _all_items(disk) if it.get("id")}
                sent_ids = {it.get("id") for _s, _n, _c, it in _all_items(payload["board"])}
                missing = sorted(disk_ids - sent_ids - removed)
                if missing:
                    return self._json(409, {"error": "stale client — the board on disk has "
                                                     f"{len(missing)} item(s) this save does not "
                                                     "contain; reload before saving",
                                            "missing": missing, "etag": text_etag(raw)})
                # Immutable Run-ID je Item (lazy-Migration) + Identitäts-Guard: ein Item,
                # dem ein Hand-Edit die @gc-id-Zeile genommen hat, bekommt sie zurück
                # statt still eine neue zu erben.
                ensure_ids(payload["board"], sidecar.SIDECAR_DIR)
                drop_arbeitsstand_on_done(disk, payload["board"])  # abgehakt → Arbeitsspeicher weg
                # Der Haken auf einem Sub ist der häufigste Erledigt-Pfad — der Roll-up
                # hängt deshalb direkt am Whole-Board-Save (idempotent, keyed by Child-ID).
                rollup_child_completions(payload["board"])
                self._atomic_write(serialize_board(payload["board"]))
                return self._json(200, {"etag": file_etag(self.board_path)})
        if self.path == "/api/gc-append":
            return self._gc_append(payload)
        if self.path == "/api/gc-body":
            return self._gc_body(payload)
        if self.path == "/api/gc-spawn-sub":
            return self._gc_spawn_sub(payload)
        if self.path == "/api/gc-run":
            return self._gc_run(payload)
        if self.path == "/api/gc-run-all":
            return self._gc_run_all(payload)
        if self.path == "/api/gc-compact":
            return self._gc_compact(payload)
        if self.path == "/api/gc-stop":
            return self._gc_stop(payload)
        if self.path == "/api/onboarding-close":
            return self._onboarding_close(payload)
        if self.path == "/api/quick-capture":
            return self._quick_capture(payload)
        if self.path == "/api/testrig":
            code, body = start_testrig(str(payload.get("mode", "fresh")))
            return self._json(code, body)
        if self.path == "/api/action-run":
            return self._action_run(payload)
        if self.path == "/api/chat-send":
            return self._chat_send(payload)
        if self.path == "/api/triage-run":
            # ⟳-Button ["wenn ich refresh klicke wird auch neu generiert"] —
            # gleicher Lauf wie der Cron, nur sofort. 409 solange einer läuft.
            claude_cmd = claude_binary()
            if not start_triage(self.board_path, claude_cmd):
                return self._json(409, {"error": "Triage is already running"})
            return self._json(202, {"ok": True})
        if self.path == "/api/triage-snooze":
            return self._triage_snooze(payload)
        if self.path == "/api/ritual-done":
            return self._ritual_done(payload)
        if self.path == "/api/ritual-snooze":
            return self._ritual_snooze(payload)
        if self.path == "/api/gate-override":
            return self._gate_override(payload)
        if self.path == "/api/gc-terminal":
            return self._gc_terminal(payload)
        return self._json(404, {"error": "not found"})

    def _onboarding_close(self, payload: dict) -> None:
        """Archive the completed starter topic after its final thread is closed.

        The closer cannot delete its own topic from inside the agent run: the runner
        still needs that item to append its reply. The UI therefore marks the card Done,
        closes its thread through the normal path, and only then calls this endpoint.
        """
        gc_id = (payload.get("id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{6,32}", gc_id):
            return self._json(400, {"error": "bad id"})
        import sweep  # Local import avoids server <-> sweep work during startup.

        ok, note, count = sweep.archive_completed_theme(
            "Getting started",
            gc_id,
            self.board_path,
            self.board_path.parent / "board-archive.md",
            self.board_path.parent / "gc-threads",
            self.board_path.parent / "gc-threads" / "archive",
        )
        return self._json(200 if ok else 409, {"ok": ok, "note": note, "archived": count})

    def _gc_terminal(self, payload: dict) -> None:
        """Read-only-Terminal auf die Agenten-Session eines Items öffnen/schließen.

        Die Ansicht ist ein RESUME-Terminal, kein Spiegel: sie startet
        `claude --resume <uuid>` (bzw. das Gegenstück des jeweiligen Runners) auf
        derselben Session und rendert deren Historie. Ein gerade laufender Board-Run
        wird davon nicht gespiegelt — und weil nur gelesen wird, auch nicht gestört.

        Der Server hält hier bewusst keinen Zustand: `terminal.py` kennt seinen
        einen Betrachter selbst (Zustandsdatei), damit ein Server-Neustart keinen
        verwaisten ttyd hinterlässt, den niemand mehr zuordnen kann.
        """
        import gc_runner                                  # lokal wie die übrigen Runner-Zugriffe
        import terminal

        if (payload.get("action") or "") == "close":
            return self._json(200, terminal.close())

        gc_id = (payload.get("id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{6,32}", gc_id):
            return self._json(400, {"error": "bad id"})
        # Live gefunden: der Knopf würde sonst ein `--resume` auf die Session legen, die
        # der laufende Run gerade selbst schreibt. Lesen ist harmlos, aber zwei Prozesse
        # auf einer Session-ID sind der gefährlichste Punkt des ganzen Vorhabens — und ab
        # einem Schreibmodus wäre es ein zerlegter Verlauf. Also gar nicht erst anbieten.
        if gc_id in RUNNING:
            return self._json(409, {"error": "A run is active on this item — "
                                             "watch the event stream instead"})
        board = parse_board(self.board_path.read_text())
        hit = next((it for _s, _n, _c, it in _all_items(board) if it.get("id") == gc_id), None)
        if hit is None:
            return self._json(409, {"error": "item not found"})
        session = (hit.get("session") or "").strip()
        handle = gc_runner.session_uuid(session)          # nicht `uuid` — Modulname
        if not handle:
            return self._json(409, {"error": "This item has no agent session yet"})
        try:
            return self._json(200, terminal.open_terminal(
                gc_id, gc_runner.session_runner(session), handle))
        except terminal.TerminalError as exc:
            return self._json(503, {"error": str(exc)})

    def _gc_stop(self, payload: dict) -> None:
        """Des Owners „esc" (2026-07-27): laufenden Agent-Run abbrechen.

        Wir killen NICHT von hier aus. Der Server legt nur eine Stopp-Marke, die
        Laufzeit-Wache im Runner sieht sie (Takt: POLL_EVERY) und beendet den Prozess
        selbst. Damit weiß der Abbrechende auch, WARUM abgebrochen wurde — und der Faden
        bekommt „⏹ Von dir gestoppt nach X min" plus den Session-Handle, statt eines
        ❌, das nach Absturz aussieht. Fortsetzen geht danach per normaler @gc:-Nachricht.
        """
        gc_id = (payload.get("id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{6,32}", gc_id):
            return self._json(400, {"error": "bad id"})
        err = request_stop(gc_id)
        if err:
            return self._json(409, {"error": err})
        return self._json(202, {"ok": True, "note": "Stop requested — the run is cleaning up"})

    def _ritual_done(self, payload: dict) -> None:
        """Ritual abhaken (Füttern): Proof-Pflichtfeld + Journal-Append; bei
        `persist_personal` zusätzlich append-only in die Ziel-Datei (NIE überschreiben —
        die Datei existiert bereits, z.B. die Therapie-Ablage der Reflection).
        proof-kind "none": Server kanonisiert den Proof-Text selbst statt
        dem Client zu vertrauen — ein Client kann so weder beliebigen Text einschleusen noch
        von der Konstante abweichen. Zusätzlich idempotent: ein zweites done im selben Zyklus
        (Doppelklick, One-Click-Pfad ohne Modal-Submit-Lock) häng kein zweites Event an."""
        rid = (payload.get("id") or "").strip()
        cfg = (load_rituale().get("rituale") or {}).get(rid)
        if cfg is None:
            return self._json(404, {"error": f"Unknown ritual: {rid or '(empty)'}"})
        proof_kind = cfg.get("proof", "single")
        if proof_kind == "none":
            proof = "(no proof required)"
        else:
            proof = (payload.get("proof") or "").strip()
            if not proof:
                return self._json(400, {"error": "proof must not be empty"})
        now = ritual_now()
        _appear, _deadline, cycle = _ritual_cycle(cfg, now)
        with RITUAL_LOCK:
            already = any(e.get("ritual") == rid and e.get("cycle") == cycle and e.get("kind") == "done"
                          for e in _read_jsonl(RITUAL_JOURNAL))
            if already:
                return self._json(200, {"ok": True, "already_done": True})
            _append_jsonl(RITUAL_JOURNAL, {"ritual": rid, "cycle": cycle, "kind": "done",
                                           "ts": now.isoformat(timespec="seconds"), "proof": proof})
        if rel := cfg.get("persist_personal"):
            p = Path(rel)
            p = p if p.is_absolute() else (GC_ROOT / rel)
            try:
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(f"## {now.date().isoformat()}\n{proof}\n\n")
            except OSError as e:  # noqa: BLE001 — Persist ist nice-to-have, darf den Haken nie blocken
                print(f"todo-board: ritual persist_personal fehlgeschlagen ({rid}): {e}", file=sys.stderr)
        return self._json(200, {"ok": True})

    def _triage_snooze(self, payload: dict) -> None:
        """+1h / +1d an einer Triage-Zeile. Fasst board.md nicht an — das Item bleibt
        unverändert, nur die Anzeige lässt es eine Weile aus."""
        iid = (payload.get("id") or "").strip()
        try:
            hours = float(payload.get("hours") or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if not iid:
            return self._json(400, {"error": "id is missing"})
        if hours not in TRIAGE_SNOOZE_HOURS:
            return self._json(400, {"error": "hours must be one of: "
                                             + ", ".join(str(h) for h in TRIAGE_SNOOZE_HOURS)})
        try:
            until = triage_snooze(iid, hours)
        except OSError as e:
            return self._json(500, {"error": f"snooze not writable: {e}"})
        return self._json(200, {"ok": True, "until": until})

    def _ritual_snooze(self, payload: dict) -> None:
        """+1h Snooze — max 1× pro Ritual/Zyklus (409 beim zweiten Versuch)."""
        rid = (payload.get("id") or "").strip()
        cfg = (load_rituale().get("rituale") or {}).get(rid)
        if cfg is None:
            return self._json(404, {"error": f"Unknown ritual: {rid or '(empty)'}"})
        now = ritual_now()
        _appear, _deadline, cycle = _ritual_cycle(cfg, now)
        journal = _read_jsonl(RITUAL_JOURNAL)
        if any(e.get("ritual") == rid and e.get("cycle") == cycle and e.get("kind") == "snooze"
               for e in journal):
            return self._json(409, {"error": "Already snoozed once today"})
        new_deadline = now + timedelta(hours=RITUAL_SNOOZE_HOURS)
        event = {"ritual": rid, "cycle": cycle, "kind": "snooze",
                 "ts": now.isoformat(timespec="seconds"),
                 "new_deadline": new_deadline.isoformat(timespec="minutes")}
        _append_jsonl(RITUAL_JOURNAL, event)
        return self._json(200, {"ok": True, "snoozed_until": event["new_deadline"]})

    def _gate_override(self, payload: dict) -> None:
        """„Trotzdem" nach dem 40s-Countdown (clientseitig erzwungen) — zählbares
        Journal-Event, beruhigt das Gate global für GATE_OVERRIDE_SILENCE_MIN."""
        gate = (payload.get("gate") or "").strip()
        if gate != "ritual":
            return self._json(400, {"error": "Unknown gate"})
        now = ritual_now()
        _append_jsonl(RITUAL_JOURNAL, {"kind": "override", "gate": "ritual",
                                       "ts": now.isoformat(timespec="seconds")})
        return self._json(200, {"ok": True})

    def _gc_append(self, payload: dict) -> None:
        """Faden-Turn anhängen OHNE Whole-Board-Overwrite: unter Lock frisch
        einlesen → Item per Fingerprint finden → Event anhängen → atomar schreiben.
        Damit kann ein paralleler Board-Save keine gerade angehängten Turns fressen.

        ZWEI WEGE (seit 07.08., Vorfall 28.07.):
        - Board sauber → unverändert der alte Pfad: ganze Datei reserialisieren.
          Das ist der Normalfall und behält Normalisierung + Sub-Roll-up.
        - Board hat ungeparste Zeilen → chirurgisch: nur der Zeilenblock DIESES
          Items wird ersetzt, der Rest bleibt byteidentisch. Ein Defekt an Item A
          hält die Antwort an Item Z damit nicht mehr auf; 409 gibt es nur noch,
          wenn das ZIELITEM selbst nicht verlustfrei round-trippt.
        """
        requested_kind = payload.get("kind")
        # "radar" is only an atomic write wish, not a persisted turn kind: normally
        # it becomes a native agent reply (reply + unread). If an owner turn is
        # already waiting, the finding stays a sys context turn so it doesn't
        # falsely mark the open ask as answered.
        if requested_kind not in ("ask", "reply", "done", "sys", "radar"):
            return self._json(400, {"error": "bad kind"})
        raw_text = (payload.get("text") or "").strip()
        addr = payload.get("addr") or {}
        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            block: tuple[int, int] | None = None
            if lost_total(raw, board) == 0:
                matches = find_item(board, addr)
                if len(matches) != 1:
                    return self._json(409, {"error": "item not uniquely found",
                                            "matches": len(matches),
                                            "etag": file_etag(self.board_path)})
                it = matches[0]
            else:
                found = locate_item_block(raw, addr)
                if found is None:
                    # Auf einem kaputten Board ist "nicht gefunden" oft eine FOLGE des
                    # Defekts (eine doppelte @gc-id verschiebt die Identität) — deshalb
                    # hier der Hinweis aufs Lint statt eines nackten "not found".
                    return self._json(409, {"error": "Target item is not uniquely identifiable "
                                                     "in the source — the board has unparsed "
                                                     "lines (board_lint.py shows where)",
                                            "matches": 0,
                                            "etag": file_etag(self.board_path)})
                start, end, it = found
                if item_lines(it) != raw.split("\n")[start:end]:
                    return self._json(409, {
                        "error": f"Item '{it.get('title', '')}' has unparsed lines — "
                                 "appending would destroy them",
                        "etag": file_etag(self.board_path)})
                block = (start, end)
            if not it.get("id"):  # damit /api/gc-pending nie id="" liefert
                it["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
            kind = requested_kind
            if requested_kind == "radar":
                kind = "sys" if thread_status(it) == "for_gc" else "reply"
            # board.md-Diät (2026-07-16): lange/mehrzeilige Turns wandern KOMPLETT in
            # einen Sidecar (inbox/gc-threads/), inline bleibt Kurzsatz + Verweis — symmetrisch
            # zur Antwort-Regel im Runner (vorher deckte die nur @gc-re: und des Owners
            # Paste-backs machten 52 % der board.md-Bytes aus). Für kurze Einzeiler ist
            # inline_turn ein No-op; die ·-Normalisierung bleibt als Gürtel — Faden-Events
            # sind Markdown-EINZEILER, eingebettete Umbrüche würden beim nächsten Parse
            # als ungetaggte Zeilen still verworfen.
            text = re.sub(r"\s*\n+\s*", " · ",
                          sidecar.inline_turn(it["id"], it.get("title", ""), raw_text,
                                              self.board_path.parent / "gc-threads", kind=kind).strip())
            it["thread"].append({"kind": kind, "text": text})
            if session := (payload.get("session") or "").strip():
                _retire_session(it, it.get("session", ""), session)  # alte UUID vor dem Überschreiben sichern
                it["session"] = session  # Resume-Pointer, im selben atomaren Write
            if gc_last := (payload.get("gc_last") or "").strip():
                it["gc_last"] = gc_last  # Run-Meta (Kontextgröße + Zeitpunkt), selber Write
            if block is None:
                # Zweiter Erledigt-Pfad: ein Hand-Edit in board.md hat ein Sub abgehakt, ohne
                # dass die UI je einen Whole-Board-Save geschickt hätte. Derselbe idempotente
                # Handler — er findet nur, was noch keinen Roll-up hat.
                rollup_child_completions(board)
                self._atomic_write(serialize_board(board))
            else:
                # Chirurgisch: Roll-up bewusst ausgelassen — er schreibt FREMDE Items,
                # das ginge nur über den Whole-Board-Weg. Er ist idempotent und holt es
                # beim nächsten sauberen Save nach.
                self._atomic_write(splice_item_block(raw, block[0], block[1], it))
            return self._json(200, {"etag": file_etag(self.board_path), "ok": True,
                                    "id": it["id"], "kind": kind})

    def _gc_body(self, payload: dict) -> None:
        """Replace body and/or append a stage without a board.md hand-splice.

        The path is deliberately item-local and has two safety tiers:

        * canonical file -> mutate the dict and reserialise it whole;
        * non-canonical file -> replace only the byte-stable round-tripping
          target block, everything outside stays verbatim.

        Body replaces additionally require ``bodyEtag``: flock prevents colliding
        file writes, but not a logically stale whole-body replace. Stage appends,
        on the other hand, read fresh under the lock and are idempotent by raw
        text; they need no revision.
        """
        addr = payload.get("addr") or {}
        if not isinstance(addr, dict):
            return self._json(400, {"error": "addr must be an object"})
        has_body = "body" in payload
        has_stage = "stage" in payload
        if not has_body and not has_stage:
            return self._json(400, {"error": "body or stage is required"})

        body: list[str] | None = None
        expected_body_etag = ""
        if has_body:
            raw_body = payload.get("body")
            if isinstance(raw_body, str):
                candidate = raw_body.splitlines()
            elif isinstance(raw_body, list) and all(isinstance(line, str) for line in raw_body):
                candidate = raw_body
            else:
                return self._json(400, {"error": "body must be a string or a list of strings"})
            if any("\n" in line or "\r" in line for line in candidate):
                return self._json(400, {"error": "body list entries must be single lines"})
            # Empty markdown paragraph lines are not a body value in the existing
            # data model (parse_board ignores them). Normalise instead of
            # rejecting the most common --body-file case with a useless error.
            body = [line.rstrip() for line in candidate if line.strip()]
            expected_body_etag = str(payload.get("bodyEtag") or "").strip()
            if not expected_body_etag:
                return self._json(400, {"error": "bodyEtag is required when replacing body"})

        stage = None
        if has_stage:
            raw_stage = payload.get("stage")
            if not isinstance(raw_stage, str) or "\n" in raw_stage or "\r" in raw_stage:
                return self._json(400, {"error": "stage must be one line"})
            raw_stage = raw_stage.strip()
            if raw_stage.lower().startswith("@stage:"):
                return self._json(400, {"error": "stage is the text after @stage:, not the tag itself"})
            stage = _parse_stage(raw_stage)
            if stage is None:
                return self._json(400, {"error": "stage must contain a stage value"})

        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            canonical = serialize_board(board) == raw
            found = locate_item_block(raw, addr)
            if found is None:
                return self._json(409, {"error": "item not uniquely found", "matches": 0,
                                        "etag": text_etag(raw)})
            start, end, block_item = found
            if item_lines(block_item) != raw.split("\n")[start:end]:
                return self._json(409, {
                    "error": f"Item '{block_item.get('title', '')}' has unparsed or non-canonical lines — "
                             "body write would destroy or reorder them",
                    "etag": text_etag(raw),
                })

            if canonical:
                matches = find_item(board, addr)
                if len(matches) != 1:
                    return self._json(409, {"error": "item not uniquely found",
                                            "matches": len(matches), "etag": text_etag(raw)})
                it = matches[0]
            else:
                # The overall file is not byte-canonical (lost_total==0 can still
                # just mean reorderable hand text). Touch only the verified
                # block; foreign ordering stays byte-identical.
                it = block_item

            current_body_etag = item_body_etag(it.get("body", []))
            if body is not None and expected_body_etag != current_body_etag:
                return self._json(409, {"error": "item body changed since this run started",
                                        "bodyEtag": current_body_etag, "etag": text_etag(raw)})

            changed = False
            if body is not None and body != it.get("body", []):
                it["body"] = body
                changed = True
            if stage is not None and not any(
                    sev.get("text", "") == stage["text"] for sev in it.get("stages", [])):
                it.setdefault("stages", []).append(stage)
                changed = True

            # The proposed body may not smuggle in a format meta line. Example: a
            # body line "@gc-id: ..." would become an attribute on the next parse
            # and vanish from body. A full item-dict comparison makes that
            # boundary explicit, including sub-checkboxes and @stage:.
            reparsed = _parse_block(item_lines(it))
            if reparsed != it:
                return self._json(400, {
                    "error": "proposed body/stage does not round-trip as the same item; "
                             "body lines may not use board metadata or sub-item syntax"
                })

            if changed:
                if canonical:
                    self._atomic_write(serialize_board(board))
                else:
                    self._atomic_write(splice_item_block(raw, start, end, it))
            return self._json(200, {"ok": True, "changed": changed, "id": it.get("id", ""),
                                    "bodyEtag": item_body_etag(it.get("body", [])),
                                    "etag": file_etag(self.board_path)})

    def _gc_spawn_sub(self, payload: dict) -> None:
        """Sub-Faden abspalten — der Schreibpfad für „das hier sind eigentlich 3
        Teilaufgaben" (Agent hat die Freiheit, Leitplanke = nicht inflationär).

        Legt ein GANZ NORMALES flaches Item direkt hinter dem Eltern-Item an, nur mit
        `@gc-parent`-Zeile — damit erbt es die komplette Maschinerie (Faden, Session,
        Wait, Run, Sweep, KPIs) gratis. Der Server bleibt einziger Writer: Agenten
        rufen diesen Endpoint (curl), statt board.md selbst umzubauen."""
        title = (payload.get("title") or "").strip()
        parent_id = (payload.get("parent_id") or payload.get("parent") or "").strip()
        ask = (payload.get("ask") or "").strip()
        if not title or not parent_id:
            return self._json(400, {"error": "title and parent_id are required"})
        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            if lost_total(raw, board) > 0:
                return self._json(409, {"error": "The board has unparsed lines — save blocked",
                                        "etag": file_etag(self.board_path)})
            idx = item_index(board)
            par = idx.get(parent_id)
            if par is None:
                return self._json(404, {"error": f"No item with @gc-id {parent_id}"})
            if par.get("parent"):
                # Tiefen-Guard: genau eine Ebene. Ein Sub eines Subs würde die Kante
                # ohnehin ungültig machen (parent_of), hier scheitert er ehrlich und laut.
                return self._json(409, {"error": "Only one level is allowed: this item is already a "
                                                 f"sub-thread (parent {par['parent']})"})
            # Liste + Position des Elternitems finden — das Sub landet direkt dahinter.
            # Identitätsvergleich (`is`), nicht `list.index`: zwei Items mit gleichem
            # Titel/Datum sind als Dicts gleich und würden die Position verfälschen.
            def locate(lst: list[dict]) -> int | None:
                return next((i for i, x in enumerate(lst) if x is par), None)
            target = pos = None
            for lst in ([th["cols"].get(col, []) for th in board["themes"] for col in theme_cols(th)]
                        + [p["items"] for p in board["persons"]]):
                if (i := locate(lst)) is not None:
                    target, pos = lst, i
                    break
            if target is None:
                return self._json(409, {"error": "Parent item is not in any column or people list"})
            child = _new_item(False, title)
            child["date"] = date.today().isoformat()
            child["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
            child["parent"] = parent_id
            if ask:
                child["thread"].append({"kind": "ask", "text": re.sub(
                    r"\s*\n+\s*", " · ",
                    sidecar.inline_turn(child["id"], title, ask,
                                        self.board_path.parent / "gc-threads", kind="ask").strip())})
            target.insert(pos + 1, child)
            self._atomic_write(serialize_board(board))
            return self._json(200, {"ok": True, "id": child["id"], "parent": parent_id,
                                    "etag": file_etag(self.board_path)})

    def _gc_run(self, payload: dict) -> None:
        """Board-Agent starten: headless claude (Auto-Mode) in einem Daemon-Thread.
        Der Runner schreibt sein Ergebnis über /api/gc-append zurück — der Server
        bleibt der einzige board.md-Writer (Single-Writer-Disziplin)."""
        import gc_runner  # lazy — der Server soll auch ohne Runner-Kontext starten
        gc_id = (payload.get("id") or "").strip()
        addr = payload.get("addr") or {}
        if not gc_id and not addr:
            return self._json(400, {"error": "id or addr is missing"})
        try:
            timeout = int(payload.get("timeout") or gc_runner.DEFAULT_TIMEOUT)
        except (TypeError, ValueError):  # VOR der Registry validieren — sonst bliebe RUNNING hängen
            return self._json(400, {"error": "timeout must be a number"})
        model = (payload.get("model") or "").strip()
        if model not in MODEL_CHOICES:
            return self._json(400, {"error": f"model must be one of: {', '.join(m or 'default' for m in MODEL_CHOICES)}"})
        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            cand = [(s, n, c, it) for s, n, c, it in _all_items(board)
                    if gc_id and it.get("id") == gc_id]
            if not cand and addr:  # Fallback: Item ohne @gc-id (z.B. Hand-Edit) per Fingerprint
                hits = find_item(board, addr)
                cand = [(s, n, c, it) for s, n, c, it in _all_items(board)
                        if any(it is h for h in hits)]
            if len(cand) != 1:
                return self._json(409, {"error": "item not uniquely found", "matches": len(cand)})
            s, n, c, it = cand[0]
            if thread_status(it) != "for_gc":
                return self._json(409, {"error": "Item is not waiting for GC (latest turn must be @gc:)"})
            if not it.get("id"):  # fehlende Run-Identität jetzt vergeben + persistieren
                if lost_total(raw, board) > 0:
                    return self._json(409, {"error": "The board has unparsed lines — save blocked"})
                it["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
                self._atomic_write(serialize_board(board))
            gc_id = it["id"]
        pending = pending_entry(s, n, c, it, board)
        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        claude_cmd = claude_binary()  # Test-Hook (Fake-Binary)
        if not launch_gc_run(pending, base_url, claude_cmd, timeout, model=model):
            # launch_gc_run hat zwei Absagegründe, und die fühlen sich völlig verschieden
            # an: „läuft schon" (nichts tun) vs. Neustart-Drain (nach dem Tausch nochmal
            # drücken). Dasselbe Muster wie beim Action-Start (s. run_cockpit_action).
            return self._json(409, {"error": RESTART_DRAIN_MSG if restart_draining()
                                    else "A run for this item is already in progress"})
        return self._json(202, {"ok": True, "id": gc_id, "model": model or "default"})

    def _gc_compact(self, payload: dict) -> None:
        """Kontext-Saver (2026-07-16, Overlay-Blatt Q4=A): schickt `/compact` an die
        bestehende Agent-Session — der sanfte Hebel neben „Faden schließen": die Session
        läuft weiter, Claude fasst den bisherigen Verlauf zusammen. Headless verifiziert
        (claude --resume <uuid> -p "/compact"). Läuft als Daemon-Thread über die
        RUNNING-Registry (blockt Doppel-Runs aufs selbe Item); COMPACTING lässt die UI
        „kompaktiert…" statt „Agent läuft" anzeigen. Erfolg stempelt @gc-last, Fehler
        wird als ❌-Reply im Faden sichtbar (fail gracefully). Kein Journal: bricht der
        Server mitten drin ab, ist schlimmstenfalls die Statusmeldung weg — die
        Kompaktierung selbst passiert in claudes Session-Store."""
        import gc_runner
        gc_id = (payload.get("id") or "").strip()
        if not gc_id:
            return self._json(400, {"error": "id is missing"})
        board = parse_board(self.board_path.read_text())
        hit = next((it for _s, _n, _c, it in _all_items(board) if it.get("id") == gc_id), None)
        if hit is None:
            return self._json(409, {"error": "item not found"})
        resume_id = gc_runner.session_uuid(hit.get("session", ""))
        if not resume_id or gc_runner.session_cut(hit.get("thread", [])):
            return self._json(409, {"error": "No active agent session — nothing to compact"})
        runner = gc_runner.session_runner(hit.get("session", ""))
        if runner == "codex":
            # Codex has no verified headless equivalent of Claude Code's `/compact` command.
            # Sending the Claude command through a different CLI would at best be a normal chat
            # turn and at worst mutate the wrong session, so fail visibly until it is measured.
            return self._json(409, {"error": "Codex session compaction is not supported yet"})
        with RUN_LOCK:
            if gc_id in RUNNING or gc_id in QUEUED:
                return self._json(409, {"error": "A run for this item is already in progress"})
            RUNNING[gc_id] = time.time()
            COMPACTING.add(gc_id)
        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        claude_cmd = claude_binary()
        board_path = self.board_path

        def work() -> None:
            try:
                out = gc_runner.spawn_claude("/compact", resume_id, claude_cmd,
                                             timeout=COMPACT_TIMEOUT)
                if out["ok"]:
                    stamp = time.strftime("%Y-%m-%d %H:%M")
                    set_gc_last(board_path, gc_id, f"kompaktiert · {stamp}")
                else:
                    gc_runner._post_append(base_url, gc_id,
                                           "❌ Compaction failed: "
                                           + (out["raw_error"] or out["reply"] or "unknown error"), "")
            except Exception as e:  # noqa: BLE001 — fail gracefully, nie stumm crashen
                print(f"todo-board: compaction for {gc_id} crashed: {e}")
            finally:
                with RUN_LOCK:
                    RUNNING.pop(gc_id, None)
                    COMPACTING.discard(gc_id)

        threading.Thread(target=work, daemon=True).start()
        return self._json(202, {"ok": True, "id": gc_id})

    def _gc_run_all(self, payload: dict) -> None:
        """Alle wartenden ⏳GC-Items abarbeiten — max `limit` (default 2) parallel.
        Ein Koordinator-Thread füttert launch_gc_run über ein Semaphor; vor jedem
        Start wird das Item re-validiert (Antwort kann inzwischen gelandet sein)."""
        import gc_runner
        try:
            limit = max(1, min(3, int(payload.get("limit") or 2)))
            timeout = int(payload.get("timeout") or gc_runner.DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return self._json(400, {"error": "limit/timeout must be a number"})
        model = (payload.get("model") or "").strip()
        if model not in MODEL_CHOICES:
            return self._json(400, {"error": f"model must be one of: {', '.join(m or 'default' for m in MODEL_CHOICES)}"})
        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            # Wartenden Items ohne @gc-id JETZT eine geben — sonst fielen sie still aus
            # der Queue (Items bekommen ihre id sonst erst bei Save/Append/Einzel-Run).
            if any(thread_status(it) == "for_gc" and not it.get("id") for _s, _n, _c, it in _all_items(board)):
                if lost_total(raw, board) > 0:
                    return self._json(409, {"error": "The board has unparsed lines — save blocked"})
                ensure_ids(board, sidecar.SIDECAR_DIR)
                self._atomic_write(serialize_board(board))
        with RUN_LOCK:
            active = set(RUNNING) | set(QUEUED)
        # Cockpit-Pseudo-Items bewusst NICHT im Run-all: Actions sind klick-getriggert,
        # und auth-Actions (Browser/Login) verlangen den Bestätigungs-Klick in der UI —
        # ein Sammel-Run würde den umgehen. (gc-pending behält sie: Journal-Recovery.)
        pend = [it["id"] for s, _n, _c, it in _all_items(board)
                if s != "cockpit" and thread_status(it) == "for_gc"]
        ids = [i for i in pend if i not in active]
        skipped = len(pend) - len(ids)
        if not ids:
            return self._json(200, {"ok": True, "queued": [], "skipped_active": skipped, "limit": limit})
        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        claude_cmd = claude_binary()
        board_path = self.board_path
        with RUN_LOCK:  # sofort als "wartet" sichtbar — Transparenz für die UI-Pill
            for gid in ids:
                QUEUED[gid] = time.time()

        def coordinator() -> None:
            sem = threading.Semaphore(limit)
            for gid in ids:
                sem.acquire()
                with RUN_LOCK:
                    QUEUED.pop(gid, None)
                b = parse_board(board_path.read_text())
                pe = next((pending_entry(s, n, c, it, b) for s, n, c, it in _all_items(b)
                           if it.get("id") == gid and thread_status(it) == "for_gc"), None)
                if pe is None:  # inzwischen beantwortet/geschlossen → überspringen
                    sem.release()
                    continue
                launch_gc_run(pe, base_url, claude_cmd, timeout, semaphore=sem, model=model)

        threading.Thread(target=coordinator, daemon=True).start()
        return self._json(202, {"ok": True, "queued": ids, "skipped_active": skipped,
                                "limit": limit, "model": model or "default"})

    def _action_run(self, payload: dict) -> None:
        """Quick Action ausführen (E3) — dünne HTTP-Hülle um `run_cockpit_action`.
        Die Fachlogik ist bewusst modulweit statt hier lokal: eine Action kann auch von
        einem eigenen Trigger-Thread statt vom HTTP-Handler gestartet werden."""
        code, out = run_cockpit_action(
            self.board_path, (payload.get("key") or "").strip(),
            f"http://127.0.0.1:{self.server.server_address[1]}",
            claude_binary(),
            model=(payload.get("model") or "").strip())
        return self._json(code, out)

    CHAT_MISSION = (
        f"You are the to-do board's daily cockpit chat (the thread lasts one day; tomorrow starts "
        f"a fresh one). Your core job is to turn {_cfg.OWNER}'s messages into to-dos: add each item "
        "DIRECTLY to inbox/board.md in the appropriate theme and column, using the existing item "
        "format '- [ ] Title *(YYYY-MM-DD)*'. Choose the category yourself and say where you put "
        "it. Only when genuinely unclear, use the 'Inbox' theme and ask one short follow-up. Prefer "
        "ONE targeted question to a form. Handle everything else (questions, short research) like "
        "a normal session: compactly; this thread is a chat, not documentation.")

    def _chat_send(self, payload: dict) -> None:
        """Cockpit-Chat (E5): Tages-Pseudo-Item 'Chat YYYY-MM-DD' in '# Cockpit' —
        Tageswechsel = neues Item = natürlicher Kontext-Schnitt (frische Session).
        Senden = @gc:-Turn (mit Sidecar-Diät wie überall) + sofortiger Agent-Run.
        Ersetzt im Cockpit die Schnellerfassung; Modell default Opus."""
        import gc_runner
        text = (payload.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "text is missing"})
        model = (payload.get("model") or "opus").strip()
        if model not in MODEL_CHOICES:
            return self._json(400, {"error": f"model must be one of: {', '.join(m or 'default' for m in MODEL_CHOICES)}"})
        today = time.strftime("%Y-%m-%d")
        marker = f"chat:{today}"
        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            if lost_total(raw, board) > 0:
                return self._json(409, {"error": "The board has unparsed lines — chat blocked"})
            item = next((it for it in board.get("cockpit", []) if marker in it.get("body", [])), None)
            if item is None:
                item = _new_item(False, f"Chat {today}")
                item["date"] = today
                item["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
                item["body"] = [marker, "···", self.CHAT_MISSION]
                board.setdefault("cockpit", []).append(item)
            turn = re.sub(r"\s*\n+\s*", " · ",
                          sidecar.inline_turn(item["id"], item["title"], text,
                                              self.board_path.parent / "gc-threads", kind="ask").strip())
            item["thread"].append({"kind": "ask", "text": turn})
            self._atomic_write(serialize_board(board))
            gc_id = item["id"]
            pending = pending_entry("cockpit", "Cockpit", None, item)
        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        claude_cmd = claude_binary()
        if not launch_gc_run(pending, base_url, claude_cmd, gc_runner.DEFAULT_TIMEOUT, model=model) \
                and restart_draining():
            return self._json(409, {"error": RESTART_DRAIN_MSG, "id": gc_id})
        return self._json(202, {"ok": True, "id": gc_id})

    def _quick_capture(self, payload: dict) -> None:
        """Schnellerfassung: Freitext -> sofort ein neues Item mit dem Text als erstem
        @gc:-Turn UND direkt ein Board-Agent-Run drauf, kein Formular dazwischen.

        Zielthema: explizit per ``theme``/``col`` -- der Spalten-Adder uebergibt seine
        eigene Zelle, wenn man ihm eine Karte mit Cmd/Strg+Enter an den Agenten gibt.
        Ohne Angabe zuerst ein VORHANDENES Thema "Inbox", sonst das ERSTE Thema des
        Boards. Ein neues Thema wird NIE angelegt: das zerriss auf einer frischen
        Installation den bewusst flachen Start mit wenigen Kategorien.
        "Inbox" bleibt nur ein Fangkorb: der Prompt-Contract erlaubt dem Agenten, das Item
        umzubenennen oder in ein passendes Thema zu schieben.

        ``run=false`` erzeugt das Item, startet aber KEINEN Lauf. Das ist der Weg fuer
        "unbespielte Flaeche wird zum selbsterklaerenden To-do" (leere Cockpit-Zone): der
        Nutzer soll den Auftrag erst lesen und ergaenzen, statt sofort Tokens zu verbrennen.
        ``title`` ueberschreibt die Titel-Ableitung, ``body`` legt Kontextzeilen darunter."""
        import gc_runner
        text = (payload.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "text is missing"})
        model = (payload.get("model") or "").strip()
        if model not in MODEL_CHOICES:
            return self._json(400, {"error": f"model must be one of: {', '.join(m or 'default' for m in MODEL_CHOICES)}"})
        want_theme = (payload.get("theme") or "").strip().lower()
        want_col = (payload.get("col") or "").strip() or "Jetzt"
        if want_col not in KNOWN_COLUMNS:
            return self._json(400, {"error": f"unknown column: {want_col}"})
        do_run = payload.get("run", True) is not False
        body = payload.get("body") or []
        if isinstance(body, str):
            body = body.splitlines()
        body = [str(line).rstrip() for line in body if str(line).strip()]
        with board_write_guard(self.board_path):
            raw = self.board_path.read_text()
            board = parse_board(raw)
            if lost_total(raw, board) > 0:
                return self._json(409, {"error": "The board has unparsed lines — capture blocked"})
            named = {th["name"].strip().lower(): th for th in board["themes"]}
            theme = named.get(want_theme) if want_theme else None
            if theme is None:
                theme = named.get("inbox") or (board["themes"][0] if board["themes"] else None)
            if theme is None:  # voellig leeres Board: dann eben doch einen Fangkorb anlegen
                theme = {"name": "Inbox", "cols": {c: [] for c in DEFAULT_COLUMNS}}
                board["themes"].insert(0, theme)
            # Nur eine Spalte benutzen, die das Thema wirklich hat - sonst schriebe ein
            # setdefault("Jetzt") eine neue Spaltenueberschrift in die Datei.
            col = want_col if want_col in theme["cols"] else (
                "Jetzt" if "Jetzt" in theme["cols"] else next(iter(theme["cols"]), "Jetzt"))
            title = (payload.get("title") or "").strip()
            if not title:
                title = re.sub(r"\s+", " ", text).strip()[:60]
                if len(text) > 60:
                    title += "\u2026"
            item = _new_item(False, title)
            item["id"] = _new_id({x["id"] for _s, _n, _c, x in _all_items(board) if x.get("id")})
            if body:
                item["body"] = body
            # Gleiche Diaet-Regel wie in _gc_append: lange Captures -> Sidecar, inline Kurzsatz+Verweis.
            item["thread"] = [{"kind": "ask", "text": re.sub(
                r"\s*\n+\s*", " \u00b7 ", sidecar.inline_turn(
                    item["id"], title, text, self.board_path.parent / "gc-threads", kind="ask").strip())}]
            theme["cols"].setdefault(col, []).insert(0, item)
            self._atomic_write(serialize_board(board))
            gc_id = item["id"]
            theme_name = theme["name"]
        if not do_run:
            return self._json(201, {"ok": True, "id": gc_id, "ran": False})
        pending = pending_entry("theme", theme_name, col, item)
        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        claude_cmd = claude_binary()
        if not launch_gc_run(pending, base_url, claude_cmd, gc_runner.DEFAULT_TIMEOUT, model=model) \
                and restart_draining():
            return self._json(409, {"error": RESTART_DRAIN_MSG, "id": gc_id})
        return self._json(202, {"ok": True, "id": gc_id, "ran": True})

    def log_message(self, fmt: str, *args) -> None:  # quiet
        pass


RECOVER_EVERY = 60  # s — Journal-Wache: erntet auch verwaiste Runs, sobald deren claude fertig ist


def journal_watch(port: int) -> None:
    """Hebt liegengebliebene Agent-Antworten aus dem Run-Journal zurück ins Board.
    Läuft im Daemon-Thread: einmal kurz nach dem Start (Neustart mitten im Run — der
    Fall vom 2026-07-14, in dem eine fertige Antwort still verschwand) und danach im
    Takt, weil ein verwaister claude-Prozess erst Minuten später fertig schreibt."""
    import gc_runner
    base_url = f"http://127.0.0.1:{port}"
    time.sleep(2)  # Server muss erst lauschen — recover_journals fragt /api/gc-pending ab
    while True:
        try:
            with RUN_LOCK:
                skip = set(RUNNING)  # laufende Runs sind tabu — deren live Aufruf räumt selbst auf
            for note in gc_runner.recover_journals(base_url, skip_ids=skip):
                print(f"todo-board: {note}")
        except Exception as e:  # noqa: BLE001 — die Wache darf den Server nie mitreißen
            print(f"todo-board: journal_watch: {e}", file=sys.stderr)
        time.sleep(RECOVER_EVERY)


def serve(port: int, board: Path) -> None:
    """Bind first, watch second.

    Die Reihenfolge ist keine Kosmetik. Beide Daemons unten wirken NACH AUSSEN:
    `journal_watch` postet verwaiste Runs an `127.0.0.1:<port>`, `triage_cron` startet
    echte claude-Prozesse gegen `Handler.board_path`. Standen sie vor dem Bind, dann tat
    ein zweiter Board-Start auf einem besetzten Port genau das — er schrieb und
    verbrannte Token GEGEN DAS LAUFENDE BOARD und starb danach an einem nackten
    `OSError: [Errno 48]`-Traceback.

    Erst binden heisst: entweder der Port gehoert uns, oder wir sind schon beendet,
    bevor irgendein Daemon laeuft."""
    Handler.board_path = board.resolve()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(
                f"superboard: port {port} is already in use.\n"
                f"  Another board is probably running there — open http://localhost:{port} to see which one.\n"
                f"  Start this one elsewhere with:  superboard --port {port + 1}"
            )
        if e.errno == errno.EACCES:
            sys.exit(f"superboard: not allowed to bind port {port}. Pick a port above 1024 with --port.")
        raise
    print(f"superboard: http://localhost:{port}  →  {Handler.board_path}")
    threading.Thread(target=journal_watch, args=(port,), daemon=True).start()
    threading.Thread(target=triage_cron,
                     args=(Handler.board_path, claude_binary()),
                     daemon=True).start()
    httpd.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47822)
    ap.add_argument("--file", type=Path, default=DEFAULT_BOARD)
    args = ap.parse_args()
    serve(args.port, args.file)


if __name__ == "__main__":
    main()
