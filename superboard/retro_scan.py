#!/usr/bin/env python3
"""retro_scan.py — deterministischer Kandidaten-Finder für die Fehler-Retrospektive.

Sucht in den Board-Fäden und in den Runner-Fakten nach Stellen, an denen ein
GC-Run vermutlich etwas falsch gemacht hat. Der Scanner URTEILT NICHT — er
liefert Fundstellen mit Beleg-Pfad; die Einordnung macht der Agent-Lauf
(Cockpit-Action 🔎 Fehler-Retro).

Warum deterministisch: die 958 Faden-Dateien komplett von einem Modell lesen zu
lassen kostet ein Vielfaches und findet nicht mehr. Der Scanner engt auf ~1-2 %
ein, der Agent liest nur die.

Signale (Quelle in Klammern):
  korrektur  Owner-Turn direkt nach einer GC-Antwort mit Korrektur-Wortschatz (board.md)
  crash      GC-Antwort ist eine Fehlermeldung / Runner-Crash (board.md)
  blocked    Aktionen vom Permission-Classifier geblockt (board.md)
  schleife   viele Turns + Korrektur-Signal am selben Item = Reibungs-Schleife (board.md)
  killed     Run vom Wächter abgebrochen: cap/idle/hung/timeout (journal/killed-runs.jsonl)
  reste      Run hinterließ nicht committete neue Dateien (inbox/gc-receipts/)

Benutzung:
  python3 -m superboard.retro_scan                 # letzte 14 Tage, Markdown
  python3 -m superboard.retro_scan --since 2026-07-01
  python3 -m superboard.retro_scan --json          # maschinenlesbar
  python3 -m superboard.retro_scan --signal korrektur --limit 20
  python3 -m superboard.retro_scan --merke <gc_id> …     # als geprueft vormerken
  python3 -m superboard.retro_scan --days 10 --archiv    # regulaerer Skill-Lauf: offen + erledigt
  python3 -m superboard.retro_scan --days 10 --alle      # auch Geprueftes zeigen

Gedaechtnis: schon geprueffte Faeden sind per Default ausgeblendet (logs/retro/geprueft.jsonl).
"""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import paths as _p

ROOT = _p.GC_ROOT
BOARD = _p.BOARD
ARCHIVE = _p.ARCHIVE
THREADS = _p.THREADS
RECEIPTS = _p.RECEIPTS
KILLED = _p.JOURNAL / "killed-runs.jsonl"
GEPRUEFT = ROOT / "logs" / "retro" / "geprueft.jsonl"

# --- Korrektur-Wortschatz -------------------------------------------------
# stark = der Owner widerspricht dem Ergebnis; schwach = Reibung, kann auch
# Auftragsänderung sein. Ein Treffer allein ist kein Fehler, nur ein Kandidat.
STARK = [
    r"\bne+in\b", r"\bfalsch\b", r"stimmt (so )?nicht", r"das ist nicht",
    r"hab(e)? ich nicht gesagt", r"war nicht (der auftrag|gemeint|die frage)",
    r"wieso hast du", r"warum hast du", r"du hast (aber|doch|das) nicht",
    r"funktioniert nicht", r"geht (immer noch )?nicht", r"\bkaputt\b",
    r"\bfehler\b", r"nicht was ich", r"quatsch", r"\bunsinn\b",
    r"halluzin", r"erfunden", r"stimmt nicht",
]
SCHWACH = [
    r"nochmal", r"noch (mal|einmal)", r"immer ?noch", r"schon wieder",
    r"hattest du", r"warum nicht", r"aber ich (wollte|hatte)",
    r"das meinte ich nicht", r"nicht ganz", r"eigentlich sollte",
    r"vergessen", r"fehlt (noch|aber)", r"wo ist",
]
RE_STARK = re.compile("|".join(STARK), re.I)
RE_SCHWACH = re.compile("|".join(SCHWACH), re.I)

RE_ITEM = re.compile(r"^- \[( |x)\] (.+?)\s*$")
RE_ITEM_DATE = re.compile(r" \*\((\d{4}-\d{2}-\d{2})\)\*(?: ← .+)?\s*$")
RE_TURN = re.compile(r"^\s+@(gc|gc-re|gc-done|gc-id|gc-session|gc-sessions|gc-last):\s*(.*)$")
RE_SIDECAR = re.compile(r"→ (?:full text|full reply|voller Text|volle Antwort): (\S+\.md)")
RE_BLOCKED = re.compile(r"⚠️ \((\d+) Aktion")
RE_LAST = re.compile(r"~(\d+)k · (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) · \$([\d.]+)")
# Zurueckgepastete Antworten aus einem Entscheidungsblatt. Das sind Antworten AUF
# Fragen, kein Widerspruch gegen die vorige GC-Antwort — der Korrektur-Wortschatz
# trifft dort nur zufaellig das Thema. Im Lauf 06.08. waren so 2 der 4 schaerfsten
# Korrektur-Signale Fehlalarme, beide nur wegen des Wortes „Fehler" im Blatt-Titel.
RE_BLATT = re.compile(r"^#\s+.+—\s*Entscheidungen\s*\(\d{4}-\d{2}-\d{2}\)")

# Der Startknopf einer Cockpit-Action schreibt „▶ <Titel> ausführen" in den Faden. Das ist
# ein Trigger, kein Widerspruch — heißt die Action aber „Fehler-Retro", trifft der
# Korrektur-Wortschatz das Wort „Fehler" im Titel und die Retro meldet ihren eigenen
# Startknopf als schärfstes Signal (06.08., 4. Lauf).
RE_TRIGGER = re.compile(r"^▶\s")

# Rate-Limit der API. Trifft es, sterben ALLE parallel laufenden Runs gleichzeitig — am
# 06.08. um 17:07 gleich fünf auf einen Schlag, inklusive der Retro selbst. Das ist nie ein
# Agentenfehler, immer eine Systemgrenze; als Signal würde es nur den Budget-Deckel füllen
# und die echten Kandidaten verdrängen. Wie „Von dir gestoppt": gar nicht erst melden.
RE_LIMIT = re.compile(r"session limit|rate limit|usage limit", re.I)

SCHLEIFE_AB = 4  # ab so vielen GC-Antworten am selben Item: Iterations-Verdacht


@dataclass
class Fund:
    signal: str
    gc_id: str
    titel: str
    datum: str
    score: int
    beleg: str          # der Text, der das Signal ausgelöst hat (gekürzt)
    quelle: str         # Pfad zur Beleg-Datei (Sidecar / Receipt / killed-Stream)
    kontext: list[str] = field(default_factory=list)  # Pfade zum Faden drumherum
    zeit: str = ""      # ISO-Zeitstempel des Signals, so genau wie die Quelle ihn hergibt

    def zeitpunkt(self) -> str:
        """Vergleichbarer Zeitstempel. Ohne Uhrzeit gilt Tagesende — im Zweifel
        gilt ein Signal als NACH dem letzten Retro-Lauf, also lieber einmal zu
        viel gezeigt als ein echter Fund verschluckt."""
        return self.zeit or (f"{self.datum}T23:59:59" if self.datum else "9999")


RE_SIDECAR_ZEIT = re.compile(r"-(\d{8})-(\d{6})-[0-9a-f]{4}\.md$")


def zeit_aus_pfad(pfad: str) -> tuple[str, str]:
    """(datum, zeit) aus einem Sidecar-Dateinamen. Ohne Sidecar traegt ein
    board-Fund nur das Datum der LETZTEN Item-Aktivitaet — das kann Monate neben
    dem Signal liegen und hat im Lauf 06.08. einen Fund auf ein falsches Datum
    geschrieben. Wo ein Sidecar existiert, ist sein Zeitstempel der genauere."""
    m = RE_SIDECAR_ZEIT.search(pfad or "")
    if not m:
        return "", ""
    d, t = m.group(1), m.group(2)
    datum = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return datum, f"{datum}T{t[:2]}:{t[2:4]}:{t[4:]}"


def kurz(s: str, n: int = 220) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class Item:
    titel: str = ""
    gc_id: str = ""
    datum: str = ""
    turns: list[tuple[str, str]] = field(default_factory=list)  # (rolle, text)
    sidecars: list[str] = field(default_factory=list)
    session: str = ""
    zeit: str = ""
    cockpit: bool = False


def parse_board(pfad: Path) -> list[Item]:
    if not pfad.exists():
        return []
    items: list[Item] = []
    cur: Item | None = None
    cockpit = False
    for raw in pfad.read_text(encoding="utf-8").splitlines():
        if raw.startswith("# "):
            cockpit = raw.strip() == "# Cockpit"
        m = RE_ITEM.match(raw)
        if m:
            raw_title = m.group(2)
            dm = RE_ITEM_DATE.search(raw_title)
            datum = dm.group(1) if dm else ""
            # Das Sweep-Archiv hängt `← Thema / Spalte` HINTER das Datum. Der alte
            # Parser erwartete das Datum am Zeilenende, verlor es dadurch und ließ
            # bei `--archiv --days 10` den gesamten Altbestand ins Fenster fallen.
            titel = raw_title[:dm.start()] if dm else re.sub(r" ← .+$", "", raw_title)
            cur = Item(titel=titel.replace("**", "").strip(), datum=datum,
                       cockpit=cockpit)
            items.append(cur)
            continue
        if cur is None:
            continue
        t = RE_TURN.match(raw)
        if not t:
            continue
        marker, text = t.group(1), t.group(2)
        if marker == "gc-id":
            cur.gc_id = text.strip()
        elif marker == "gc-session":
            cur.session = text.split("·")[0].strip()
        elif marker == "gc-last":
            lm = RE_LAST.search(text)
            if lm:
                cur.zeit = f"{lm.group(2)}T{lm.group(3)}:00"
        elif marker in ("gc", "gc-re"):
            cur.turns.append((marker, text))
            sm = RE_SIDECAR.search(text)
            if sm:
                cur.sidecars.append(sm.group(1))
    return [i for i in items if i.turns]


def zeit_nahebei(turns: list[tuple[str, str]], idx: int) -> tuple[str, str, str]:
    """(quelle, datum, zeit) für einen Turn — Datum aus dem nächstgelegenen Sidecar-Verweis.

    Turns tragen selbst keinen Zeitstempel; nur ausgelagerte Langtexte
    (`… → volle Antwort: inbox/gc-threads/<id>-<datum>-<zeit>-<rnd>.md`) tun das. Ohne
    diesen Griff erbt jeder Fund das Datum der LETZTEN Item-Aktivität — an
    `f9a82be6f797` (Cockpit-Dauerläufer) erschienen so Signale vom 27.07. als 06.08., und
    der Prüfer suchte einen Tag lang am falschen Ende. Radius bewusst klein (±2 Turns):
    ein weit entferntes Sidecar wäre wieder nur geraten.

    Das NACHBAR-Sidecar ist aber nur ein Datums-Anker, kein Beleg: der Text eines
    Crash-Turns steht inline in board.md, die Nachbardatei enthält einen ANDEREN Turn.
    Am 18.08. lasen zwei Prüf-Subs deshalb eine normale Erfolgsantwort (`10af606c5850`)
    bzw. einen Owner-Turn (`6ecba2c3e110`) als „Crash-Beleg". Als Quelle kommt darum
    nur noch das Sidecar des Turns SELBST zurück; sonst ehrlich board.md.
    """
    for weite in range(3):
        for j in (idx - weite, idx + weite):
            if 0 <= j < len(turns):
                m = RE_SIDECAR.search(turns[j][1])
                if m:
                    d, z = zeit_aus_pfad(m.group(1))
                    if d:
                        return (m.group(1) if weite == 0 else "board.md"), d, z
    return "board.md", "", ""


def letzte_antwortzeit(it: Item, antworten: list[int]) -> tuple[str, str]:
    """(datum, zeit) des Zustands, den ein `schleife`-Fund beschreibt.

    Der Zustand ändert sich mit der letzten GC-Antwort. Deren Sidecar ist genauer als
    das Item-Datum und funktioniert auch für Personen-/Sondersektionen ohne `@gc-last`.
    Eine kurze Inline-Antwort ohne Sidecar darf nicht den Zeitstempel einer älteren
    Antwort erben; dann bleibt nur `@gc-last` oder der konservative Tageswert.
    """
    if antworten:
        text = it.turns[antworten[-1]][1]
        m = RE_SIDECAR.search(text)
        if m:
            d, z = zeit_aus_pfad(m.group(1))
            if z:
                return d, z
    if it.zeit:
        return it.zeit[:10], it.zeit
    return "", ""


def antwort_zaehlt_fuer_schleife(text: str, seit: date) -> bool:
    """Nur substantielle Antworten im Audit-Fenster bilden den Schleifen-Zustand.

    Ein Faden kann über Wochen wachsen. Vier Antworten insgesamt sind dann kein Signal
    für Reibung in den letzten zehn Tagen. Datiert wird aus dem eigenen Sidecar; fehlt es,
    bleibt die Antwort konservativ im Zähler. Reine Crash-Echos sind kein Arbeits-Turn.
    """
    if text.startswith(("❌", "Runner-Crash")):
        return False
    m = RE_SIDECAR.search(text)
    if not m:
        return True
    d, _ = zeit_aus_pfad(m.group(1))
    if not d:
        return True
    try:
        return date.fromisoformat(d) >= seit
    except ValueError:
        return True


def scan_board(items: list[Item], seit: date) -> list[Fund]:
    funde: list[Fund] = []
    for it in items:
        datum = it.zeit[:10] if it.zeit else it.datum
        try:
            if datum and date.fromisoformat(datum) < seit:
                continue
        except ValueError:
            pass

        antworten = [
            i for i, (r, text) in enumerate(it.turns)
            if r == "gc-re" and antwort_zaehlt_fuer_schleife(text, seit)
        ]
        hat_korrektur = False

        for idx, (rolle, text) in enumerate(it.turns):
            if rolle == "gc":
                # Korrektur zählt nur, wenn davor schon eine GC-Antwort stand —
                # sonst ist es der Auftrag, nicht die Reaktion darauf.
                if idx == 0 or it.turns[idx - 1][0] != "gc-re":
                    continue
                if RE_BLATT.match(text) or RE_TRIGGER.match(text):
                    continue
                # Themenwörter zählen nicht: heißt das Item selbst „Fehler-Retro",
                # trifft `\bfehler\b` JEDEN Owner-Turn dort — am 18.08. wurde so
                # ein Lob („… dass da vielleicht Fehler sind") zum korrektur·3-Signal.
                # Ein Treffer, dessen Wortlaut schon im Item-Titel steckt, ist Thema,
                # kein Widerspruch; alle anderen Treffer zählen weiter.
                titel_lc = it.titel.lower()
                stark = any(m.group(0).lower() not in titel_lc
                            for m in RE_STARK.finditer(text))
                schwach = any(m.group(0).lower() not in titel_lc
                              for m in RE_SCHWACH.finditer(text))
                if not (stark or schwach):
                    continue
                quelle, d_sc, z_sc = zeit_nahebei(it.turns, idx)
                funde.append(Fund(
                    signal="korrektur", gc_id=it.gc_id, titel=it.titel, datum=d_sc or datum,
                    score=3 if stark else 1, beleg=kurz(text),
                    quelle=quelle, zeit=z_sc,
                    kontext=it.sidecars[:],
                ))
                hat_korrektur = True
            else:  # gc-re
                bm = RE_BLOCKED.search(text)
                if bm:
                    quelle, d_sc, z_sc = zeit_nahebei(it.turns, idx)
                    funde.append(Fund(
                        signal="blocked", gc_id=it.gc_id, titel=it.titel, datum=d_sc or datum,
                        score=1, beleg=f"{bm.group(1)} Aktion(en) geblockt: {kurz(text, 120)}",
                        quelle=quelle, zeit=z_sc,
                    ))
                # Anker am Zeilenanfang, NICHT irgendwo im Text: eine Antwort, die einen
                # Absturz nur ERWÄHNT („die beiden Turns davor waren nur Runner-Crash-Echos"),
                # ist kein Absturz. Real gemeldet 06.08. an `4acc7737b370` — der Aufräum-Turn
                # nach dem Crash wurde als zweiter Crash gezählt. Echte Meldungen kommen aus
                # genau einer Hand (`server.py`: f"❌ Runner-Crash: {e}") und beginnen mit ❌.
                if text.startswith(("❌", "Runner-Crash")) and not RE_LIMIT.search(text):
                    quelle, d_sc, z_sc = zeit_nahebei(it.turns, idx)
                    funde.append(Fund(
                        signal="crash", gc_id=it.gc_id, titel=it.titel, datum=d_sc or datum,
                        score=2, beleg=kurz(text), quelle=quelle, zeit=z_sc,
                    ))

        # Cockpit-Items sind Dauerläufer mit persistentem Faden: viele Turns sind dort
        # der Normalzustand, nicht Reibung. Im Probelauf 06.08. war jede einzelne
        # Schleifen-Meldung an einem Cockpit-Item ein Fehlalarm (Beleg: 0bab153051b3,
        # 5 Turns = fünf reguläre Läufe der GC-Health-Action über sieben Tage).
        # Und: viele Antworten allein sind KEIN Verdacht — jede GC-Antwort setzt einen
        # Owner-Trigger voraus, reines 1:1-Ping-Pong ist vom Owner getriebene Iteration.
        # Alle 5 Schleifen-Kandidaten der Retros vom 19./20.08. waren genau das
        # (u.a. 18467360068c, 15d492062895: jede Antwort auf einen distinkten Auftrag).
        # `schleife` feuert deshalb nur noch als VERSTÄRKER, wenn derselbe Faden auch
        # Korrektur-Wortschatz trägt — viele Turns + Widerspruch = echte Reibung.
        if len(antworten) >= SCHLEIFE_AB and not it.cockpit and hat_korrektur:
            # `schleife` ist ein Zustands-Signal: es aendert sich erst mit einer neuen
            # GC-Antwort. Ohne genaue Uhrzeit galt bisher das Tagesende; ein vormittags
            # gesetzter Retro-Checkpoint servierte denselben Faden deshalb noch am selben
            # Tag erneut. `@gc-last` ist der praezise Zeitpunkt der letzten Antwort und
            # macht das Gedächtnis auch innerhalb eines Tages stabil. Alte Boards ohne
            # `@gc-last` bleiben bewusst konservativ und nutzen weiter das Tagesende.
            d_antwort, z_antwort = letzte_antwortzeit(it, antworten)
            funde.append(Fund(
                signal="schleife", gc_id=it.gc_id, titel=it.titel, datum=d_antwort or datum,
                score=2, beleg=f"{len(antworten)} GC-Antworten am selben Item",
                quelle="board.md", kontext=it.sidecars[:], zeit=z_antwort,
            ))
    return funde


def im_fenster(fund: Fund, seit: date) -> bool:
    """Funddatum ist genauer als letzte Item-Aktivität.

    Ein langlebiger Faden kann heute aktiv sein und Signale von vor Wochen enthalten.
    `scan_board()` muss ihn für die neuen Signale lesen; anschließend schneidet diese
    Prüfung die alten, per Sidecar datierten Signale wieder aus dem Retro-Fenster.
    Ohne Datum bleibt ein Fund sichtbar — lieber einmal prüfen als still verlieren.
    """
    if not fund.datum:
        return True
    try:
        return date.fromisoformat(fund.datum) >= seit
    except ValueError:
        return True


def transkript(stream: str, session_id: str, model: str = "",
               codex_sessions: Path | None = None) -> str:
    """Beleg-Pfad zum Ereignisstrom eines gekillten Runs.

    `journal/killed/` ist ein Ringpuffer — nach ein paar Wochen zeigt der Eintrag
    ins Leere (am 06.08.: 4 von 7). Die Session-UUID steht aber im selben Eintrag,
    und Claude Codes eigenes Transkript liegt dauerhaft unter ~/.claude/projects/.
    Ohne diesen Rueckfall verliert die Retro genau bei den aeltesten Faellen ihren
    Beleg — also bei denen, die niemand mehr aus dem Kopf rekonstruieren kann.

    Codex-Runs (model `codex*`, vgl. gc_runner.CODEX_PROFILES — Praefix-Check statt
    Import, retro_scan bleibt bewusst gc_runner-frei) haben ihr Pendant unter
    `~/.codex/sessions/YYYY/MM/DD/rollout-*<thread_id>.jsonl`; das Board-CODEX_HOME
    verlinkt `sessions/` dorthin, der Pfad gilt also fuer Board- wie Hand-Laeufe."""
    if stream and Path(stream).exists():
        return stream
    if session_id and (model or "").startswith("codex"):
        root = codex_sessions or (Path.home() / ".codex" / "sessions")
        try:
            hits = list(root.rglob(f"rollout-*{session_id}.jsonl"))
        except OSError:
            hits = []
        if hits:
            p = max(hits, key=lambda q: q.stat().st_mtime)
            return f"{p}  (Ringpuffer weg, Codex-Rollout)"
    elif session_id:
        slug = str(ROOT).replace("/", "-").replace(".", "-")
        p = Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"
        if p.exists():
            return f"{p}  (Ringpuffer weg, Claude-Session-Transkript)"
    return (stream or "?") + "  (Transkript nicht mehr da)"


def scan_killed(seit: date) -> list[Fund]:
    if not KILLED.exists():
        return []
    funde = []
    for zeile in KILLED.read_text(encoding="utf-8").splitlines():
        if not zeile.strip():
            continue
        try:
            e = json.loads(zeile)
        except json.JSONDecodeError:
            continue
        ts = e.get("ts", "")[:10]
        try:
            if ts and date.fromisoformat(ts) < seit:
                continue
        except ValueError:
            continue
        grund = e.get("reason", "?")
        # Der eigene Stop-Button (⏹ „Stopped by you") ist nie ein Agentenfehler —
        # der Retro-Vertrag sagt das seit je, gefiltert wurde es bis 20.08. aber nicht:
        # ein 0-Schritte-Stop (a3abed2b20ad) stand als killed·2 in der Kandidatenliste.
        if grund == "stop":
            continue
        funde.append(Fund(
            signal="killed", gc_id=e.get("gc_id", ""), titel=e.get("title", ""),
            datum=ts, score=3 if grund in ("cap", "idle", "hung") else 2,
            beleg=f"abgebrochen ({grund}) nach {e.get('elapsed_min', '?')} min, "
                  f"{e.get('steps', '?')} Schritte, zuletzt {e.get('last_tool', '?')}, {e.get('model', '?')}",
            quelle=transkript(e.get("stream", ""), e.get("session_id", ""), e.get("model", "")),
            zeit=e.get("ts", ""),
        ))
    return funde


def scan_receipts(seit: date) -> list[Fund]:
    """Receipts sind Runner-Ground-Truth: was der Lauf WIRKLICH hinterlassen hat."""
    if not RECEIPTS.exists():
        return []
    funde = []
    for p in sorted(RECEIPTS.glob("*.md")):
        teile = p.stem.split("-")
        if len(teile) < 3:
            continue
        try:
            d = datetime.strptime(teile[1], "%Y%m%d").date()
        except ValueError:
            continue
        if d < seit:
            continue
        zeit = ""
        if len(teile) >= 3 and re.fullmatch(r"\d{6}", teile[2]):
            zeit = f"{d.isoformat()}T{teile[2][:2]}:{teile[2][2:4]}:{teile[2][4:]}"
        txt = p.read_text(encoding="utf-8", errors="replace")
        titel = txt.splitlines()[0].lstrip("# ").strip() if txt else p.stem
        block = re.search(r"\*\*Nicht committet, neu seit Run-Start:\*\*.*?\n((?:\s*- `.*`\n)+)", txt)
        if block:
            # Rauschen filtern: Logs, tmp-Artefakte und Journal-Dateien liegen
            # absichtlich uncommittet herum — echtes Signal sind vergessene
            # Arbeitsergebnisse (context/, inbox/, tools/, …).
            rest = [f for f in re.findall(r"- `([^`]+)`", block.group(1))
                    if not re.match(r"(tmp/|logs/|inbox/gc-receipts/|inbox/gc-threads/|\.superboard/journal/)", f)]
            if rest:
                funde.append(Fund(
                    signal="reste", gc_id=teile[0], titel=titel, datum=d.isoformat(),
                    score=1, beleg=f"{len(rest)} neue Datei(en) nicht committet: " + ", ".join(rest[:4]),
                    quelle=str(p.relative_to(ROOT)), zeit=zeit,
                ))
        erg = re.search(r"\*\*Ergebnis:\*\* (.+)", txt)
        if erg and not erg.group(1).startswith("ok"):
            text = erg.group(1)
            # Vom Owner selbst gestoppt = kein Agentenfehler. Ebenso das API-Rate-Limit.
            if "Von dir gestoppt" in text or RE_LIMIT.search(text):
                continue
            funde.append(Fund(
                signal="crash", gc_id=teile[0], titel=titel, datum=d.isoformat(),
                score=3, beleg=f"Receipt-Ergebnis: {kurz(text, 120)}",
                quelle=str(p.relative_to(ROOT)), zeit=zeit,
            ))
    return funde


# --- Gedächtnis: was schon einmal geprüft wurde ---------------------------
# Ohne das serviert jeder Folgelauf dieselben Fäden noch einmal — im zweiten
# Retro-Lauf (06.08.) waren 5 von 6 Kandidaten bereits im ersten abgehandelt.
# Ein Eintrag heißt: "Faden X wurde bis Zeitpunkt T geprüft." Alles, was danach
# an diesem Faden passiert, taucht wieder auf; alles davor nicht.

def lade_geprueft() -> dict[str, str]:
    if not GEPRUEFT.exists():
        return {}
    bis: dict[str, str] = {}
    for zeile in GEPRUEFT.read_text(encoding="utf-8").splitlines():
        if not zeile.strip():
            continue
        try:
            e = json.loads(zeile)
        except json.JSONDecodeError:
            continue
        gid, b = e.get("gc_id", ""), e.get("bis", "")
        if gid and b:
            bis[gid] = max(bis.get(gid, ""), b)
    return bis


def merke(gc_ids: list[str], bis: str) -> None:
    GEPRUEFT.parent.mkdir(parents=True, exist_ok=True)
    with GEPRUEFT.open("a", encoding="utf-8") as f:
        for gid in gc_ids:
            f.write(json.dumps({"gc_id": gid, "bis": bis,
                                "lauf": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False) + "\n")


def letzte_aktivitaet(it: Item) -> str:
    """Jüngstes Datum, das der Faden selbst hergibt (Sidecars > @gc-last > Item-Datum)."""
    akt = ""
    for sc in it.sidecars:
        d, _ = zeit_aus_pfad(sc)
        akt = max(akt, d)
    return max(akt, it.zeit[:10] if it.zeit else "", it.datum or "")


def zufalls_pool(items: list[Item], seit: date, signal_ids: set[str],
                 bis: dict[str, str]) -> dict[str, tuple[Item, str]]:
    """Fäden OHNE jedes Fehlersignal im Fenster — Kandidaten für die Reliability-Stichprobe.

    Die Retro prüft sonst nur, wo Fehler schon aus dem Faden hervorgehen. Ein, zwei
    zufällig gezogene „scheint sauber"-Fäden pro Lauf fragen das Gegenteil: hält der
    Lauf, was er meldet? Der Scanner zieht nur — urteilen tut der billige Prüf-Sub.
    Schon geprüfte Fäden sind raus, solange sie seit dem Checkpoint keine neue
    Aktivität haben.
    """
    pool: dict[str, tuple[Item, str]] = {}
    for it in items:
        gid = it.gc_id
        if not gid or gid in signal_ids or gid in pool:
            continue
        akt = letzte_aktivitaet(it)
        try:
            if not akt or date.fromisoformat(akt) < seit:
                continue
        except ValueError:
            continue
        if gid in bis and akt <= bis[gid][:10]:
            continue
        pool[gid] = (it, akt)
    return pool


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="ab Datum YYYY-MM-DD (Default: vor 14 Tagen)")
    ap.add_argument("--days", type=int, default=14, help="Fenster in Tagen, wenn --since fehlt")
    ap.add_argument("--signal", action="append",
                    help="nur diese Signale (mehrfach möglich). Ohne Angabe: alle ausser 'reste' "
                         "— das ist das schwaechste Signal, weil parallele Board-Sessions sich "
                         "gegenseitig fremde Dateien in die Receipts schreiben.")
    ap.add_argument("--min-score", type=int, default=1)
    ap.add_argument("--limit", type=int, default=40, help="max. Funde in der Ausgabe")
    ap.add_argument("--items", type=int, metavar="N",
                    help="Budget-Deckel: nur die N auffaelligsten FAEDEN ausgeben, mit allen ihren "
                         "Signalen gebuendelt. Das ist der Hebel fuer die Kosten eines Retro-Laufs — "
                         "teuer ist das Lesen eines Original-Fadens, nicht die Zahl der Signale darin.")
    ap.add_argument("--archiv", action="store_true", help="board-archive.md mitlesen")
    ap.add_argument("--zufall", type=int, metavar="N",
                    help="zusaetzlich N zufaellige Faeden OHNE Fehlersignal ziehen "
                         "(Reliability-Stichprobe: scheint sauber — haelt der Lauf, was er "
                         "meldet?). Geprueftes ohne neue Aktivitaet ist raus; gezogene Faeden "
                         "am Ende mit --merke vormerken wie alle anderen.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alle", action="store_true",
                    help="auch schon geprueffte Faeden zeigen (Default: ausblenden, "
                         "siehe logs/retro/geprueft.jsonl)")
    ap.add_argument("--merke", nargs="+", metavar="GC_ID",
                    help="diese Faeden als geprueft vormerken (schreibt logs/retro/geprueft.jsonl "
                         "und beendet sich) — am Ende eines Retro-Laufs aufrufen")
    ap.add_argument("--bis", help="Zeitpunkt fuer --merke (Default: jetzt)")
    a = ap.parse_args()

    if a.merke:
        stand = a.bis or datetime.now().isoformat(timespec="seconds")
        merke(a.merke, stand)
        print(f"gemerkt bis {stand}: " + ", ".join(a.merke))
        return

    seit = date.fromisoformat(a.since) if a.since else date.today() - timedelta(days=a.days)

    items = parse_board(BOARD)
    if a.archiv:
        items += parse_board(ARCHIVE)

    funde = scan_board(items, seit) + scan_killed(seit) + scan_receipts(seit)
    funde = [f for f in funde if im_fenster(f, seit)]
    # Für die Zufalls-Stichprobe zählt JEDES Signal (auch 'reste', auch geprüfte):
    # „signalfrei" heißt wirklich frei, nicht nur unter den angezeigten Funden.
    signal_ids = {f.gc_id for f in funde if f.gc_id}
    erlaubt = set(a.signal) if a.signal else {"korrektur", "crash", "blocked", "schleife", "killed"}
    funde = [f for f in funde if f.signal in erlaubt]
    funde = [f for f in funde if f.score >= a.min_score]

    schon = 0
    if not a.alle:
        bis = lade_geprueft()
        vorher = len(funde)
        funde = [f for f in funde if not (f.gc_id in bis and f.zeitpunkt() <= bis[f.gc_id])]
        schon = vorher - len(funde)

    funde.sort(key=lambda f: (-f.score, f.datum or "", f.signal))

    zaehl: dict[str, int] = {}
    for f in funde:
        zaehl[f.signal] = zaehl.get(f.signal, 0) + 1
    gesamt = len(funde)

    if a.items:
        # Nach Faden buendeln und die auffaelligsten N behalten. Rang = Summe der
        # Signal-Scores; ein Faden mit drei mittleren Signalen ist verdaechtiger
        # als einer mit einem starken.
        nach_faden: dict[str, list[Fund]] = {}
        for f in funde:
            nach_faden.setdefault(f.gc_id or f.titel, []).append(f)
        rang = sorted(nach_faden.items(), key=lambda kv: -sum(x.score for x in kv[1]))
        funde = [f for _, gruppe in rang[: a.items] for f in gruppe]

    zufall: list[Fund] = []
    pool: dict[str, tuple[Item, str]] = {}
    if a.zufall:
        pool = zufalls_pool(items, seit, signal_ids, lade_geprueft())
        for gid in random.sample(sorted(pool), min(a.zufall, len(pool))):
            it, akt = pool[gid]
            zufall.append(Fund(
                signal="zufall", gc_id=gid, titel=it.titel, datum=akt, score=0,
                beleg="ohne Fehlersignal gezogen — Reliability-Stichprobe: "
                      "haelt der Lauf, was er meldet?",
                quelle="board.md", kontext=it.sidecars[:],
            ))

    if a.json:
        print(json.dumps([asdict(f) for f in funde[: a.limit] + zufall],
                         ensure_ascii=False, indent=2))
        return

    print(f"# Fehler-Retro — Kandidaten seit {seit}\n")
    print("Signale: " + (", ".join(f"{k} {v}" for k, v in sorted(zaehl.items())) or "keine")
          + (f" · Budget: {a.items} Fäden von {len({f.gc_id for f in funde}) if not a.items else len(set(nach_faden))}"
             if a.items else "") + "\n")
    if schon:
        print(f"({schon} Signale aus bereits geprueften Faeden ausgeblendet — `--alle` zeigt sie)\n")
    for f in funde[: a.limit]:
        print(f"## [{f.signal}·{f.score}] {f.datum or '?'} · {f.titel or '?'} `{f.gc_id}`")
        print(f"  {f.beleg}")
        print(f"  Beleg: {f.quelle}")
        if f.kontext:
            print(f"  Faden: {' '.join(f.kontext[-3:])}")
        print()
    if len(funde) > a.limit:
        print(f"… {len(funde) - a.limit} weitere (--limit erhöhen)")
    if a.items and gesamt > len(funde):
        print(f"\n⚠️ Budget-Deckel: {gesamt - len(funde)} Signale aus weiteren Fäden nicht gezeigt "
              f"(--items erhöhen). Der Lauf hat NICHT alles gesehen.")
    if a.zufall:
        print(f"\n# Zufalls-Stichprobe — {len(zufall)} von {len(pool)} signalfreien Fäden im Fenster\n")
        for f in zufall:
            print(f"## [zufall] {f.datum} · {f.titel or '?'} `{f.gc_id}`")
            print(f"  {f.beleg}")
            if f.kontext:
                print(f"  Faden: {' '.join(f.kontext[-3:])}")
            print()


if __name__ == "__main__":
    main()
