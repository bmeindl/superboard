#!/usr/bin/env python3
"""todo-board Done-GC — verschiebt erledigte Items (done, letzter Zustandswechsel
älter als RETENTION_HOURS) aus inbox/board.md nach inbox/board-archive.md.

Passive Müllabfuhr statt manuellem Aufräumen: der Owner sieht Erledigtes noch gut
einen Tag (morgens der Rückblick fürs Reporting), dann räumt der Roboter. Archiv
bleibt greppbar.

Zweiter Job (2026-07-14 „beides", umgebaut 2026-07-22): Wait-Eskalation. Items in
„Wartet auf andere", deren @wait seit WAIT_DECAY_DAYS nicht bestätigt wurde, BLEIBEN
dort und wandern nur an den Anfang der Spalte — Referenz und Datum bleiben erhalten,
das Badge wird rot („⏳ slim · !475 · überfällig 8d"). Bis 22.07. holte der Sweep sie
stattdessen nach „Jetzt" zurück und löschte dabei wait/wait_since; damit ging genau die
Information verloren, die den Nachfass-Impuls trägt (worauf? seit wann?). Gegen den
Friedhof schützt jetzt die Sichtbarkeit statt der Rauswurf.

Dritter Job (2026-07-16, Entscheidungsblatt Q3): Archiv-Deadlock-Auflösung.
Ein ABGEHAKTES Item mit offenem GC-Faden (Altbestand vor dem UI-Fix vom 14.07.,
Hand-Edits, Turns nach dem Haken) würde wegen open_thread() NIE archiviert —
der Sweep schließt den Faden selbst (@gc-done: mit Auto-Vermerk), VOR der
Archiv-Prüfung, damit reife Items im selben Lauf mit rausgehen.

Vierter Job (2026-07-21, Faden-Retention): wandert ein Item mit Sidecar-Dateien
(lange Faden-Turns, `inbox/gc-threads/<gc-id>-<ts>-<suffix>.md`) ins Archiv, ziehen
seine Sidecars mit nach `inbox/gc-threads/archive/` — flacher Unterordner, keine
Monatsordner. Nicht löschen, nur verschoben; Verweise im archivierten Textblock
(`→ voller Text: inbox/gc-threads/…`) werden auf den neuen Pfad umgeschrieben, damit
sie im Archiv weiter auflösen. Kollision (Zieldatei existiert schon) → überspringen +
Warnung (sichtbar in der Sweep-Ausgabe, landet damit auch im Morgen-Digest), NIE
überschreiben. Reihenfolge bewusst so: die Sidecar-Moves werden nur GEPLANT, während
board.md/board-archive.md noch nicht angefasst sind, und erst NACH deren erfolgreichem
Schreiben tatsächlich ausgeführt — ein Crash dazwischen lässt bestenfalls unverschobene
Sidecars zurück, nie Board-Zeilen mit Verweisen ins Leere. NUR Sidecars neu archivierter
Items wandern — der bestehende Live-Bestand in `inbox/gc-threads/` bleibt unangetastet.

Fünfter Job (2026-08-25): Tages-Chat-Karten im Cockpit stilllegen. Jeder Tag, an dem der
Owner den Cockpit-Chat benutzt, legt eine Karte „Chat JJJJ-MM-TT" in `# Cockpit` an — die
kein Mensch je abhakt, weil sie im Cockpit gar nicht als Aufgabe gelesen wird. Jetzt:
Chat-Karte ohne Aktivität seit CHAT_IDLE_HOURS wird abgehakt UND im selben Lauf archiviert
— die 25h-Schonfrist gilt für sie NICHT, weil die dahinter stehende Idee (Erledigtes am
nächsten Morgen noch einmal im Rückblick sehen) auf ein Chat-Protokoll nicht zutrifft.
Inhalt geht nicht verloren: Faden und Sidecars wandern wie bei jedem anderen Item ins
Archiv. Betrifft AUSSCHLIESSLICH Karten mit `chat:JJJJ-MM-TT`-Marker — die Aktions-Karten
im Cockpit sind Dauer-Items und werden hier nie angefasst.

Läuft nightly via evening-sync; manuell: python3 sweep.py [--dry-run]
Exit 0 = ok (auch wenn nichts zu tun), Exit 1 = Fehler.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import (  # noqa: E402
    GC_TAG, _all_items, board_write_guard, children_of, item_index, lost_total, parse_board,
    serialize_board, theme_cols,
)

import paths as _p  # noqa: E402

BOARD = _p.BOARD
ARCHIVE = _p.ARCHIVE
# Gleicher Ordner, den sidecar.py/server.py für Sidecar-Dateien benutzen (sidecar.SIDECAR_DIR).
SIDECAR_DIR = _p.THREADS
SIDECAR_ARCHIVE_DIR = _p.THREADS_ARCHIVE
# Stunden statt Tage (2026-07-15, "25h statt 3 Tage"): das Frontend stempelt
# beim Abhaken @done-at (UTC-ISO) — sweep.py rechnet damit auf die Stunde genau statt
# nur auf den Kalendertag. Items ohne Stempel (alte/hand-editierte Zeilen) fallen auf
# Tagesende (23:59:59 UTC) zurück, damit sie nicht vorzeitig verschwinden.
RETENTION_HOURS = 25
# 7 statt der ursprünglich angedachten 10 Tage: konsistent mit der übrigen Board-Semantik
# (Alter rot ab >7d in „Jetzt", Radar REVIEW_STALE_DAYS=7) — eine Woche unbestätigt heißt
# ohnehin: nachfassen. Bestätigen = Badge anklicken / Item neu nach „Wartet" ziehen.
# Seit 2026-07-22 die Schwelle für „überfällig" (Badge rot, oben in der Spalte), NICHT
# mehr für einen Umzug — der Name bleibt, damit Server/Frontend denselben Begriff teilen.
WAIT_DECAY_DAYS = 7
WAIT_COL = "Wartet auf andere"
# Cockpit-Tages-Chat: Marker, den server._chat_send in den Body schreibt (`chat:JJJJ-MM-TT`).
# Einzige Erkennung — Titel-Matching wäre brüchig, der Marker ist das Datenformat.
CHAT_MARKER_RE = re.compile(r"^chat:(\d{4}-\d{2}-\d{2})$")
# 3h statt der 25h-Regel für echte Items (2026-08-25): Cockpit-Chats können nach 3 Stunden
# Inaktivität schon archiviert werden. Weil der Sweep nachts läuft, heißt das praktisch:
# der Chat des Tages geht abends mit raus, sobald er ruht.
CHAT_IDLE_HOURS = 3


def gc_tag(kind: str) -> str:
    """Tag für einen Faden-Turn — aus server.GC_TAG, mit generischem Fallback.

    Doppelter Schutz gegen genau den Ausfall vom 27.–30.07.: sweep.py hielt eine eigene
    Tag-Map, die den 2026-07-27 eingeführten Kind „sys" nicht kannte. fmt_item warf KeyError,
    und weil der im Sammel-Loop fliegt, brach der GESAMTE Sweep ab — vier Nächte lang wurde
    NICHTS archiviert. Der eigentliche Defekt war nicht der fehlende Eintrag, sondern dass
    ein unbekannter Kind den ganzen Lauf killen konnte.

    Jetzt: (1) die Map kommt aus server.py, es gibt keine zweite Wahrheit mehr; (2) ein
    trotzdem unbekannter Kind wird nach der Namenskonvention zu `@gc-<kind>:` und meldet
    sich als Warnung, statt zu werfen. Der generische Pfad ist bewusst konvergent — ein
    künftiger Kind „foo" landet als `@gc-foo:` im Archiv und wäre von einem Parser, der ihn
    später lernt, wieder lesbar. Datenverlust wäre schlimmer als eine hässliche Zeile."""
    tag = GC_TAG.get(kind)
    if tag:
        return tag
    UNKNOWN_KINDS.add(kind)
    return f"@gc-{kind}:"


# Gesammelt statt sofort gedruckt: fmt_item läuft tief im Sammel-Loop, die Warnung soll am
# Ende gebündelt in der Sweep-Ausgabe stehen (und damit im Morgen-Digest).
UNKNOWN_KINDS: set[str] = set()


# Heartbeat: „wann lief der Sweep zuletzt sauber durch?" — die Lehre aus dem Ausfall
# 27.–30.07. Der Sweep starb vier Nächte still; gemerkt hat es niemand, weil der einzige
# Hinweis ein GRÖSSEN-Proxy war (board.md wächst). Der Proxy kann nicht zwischen „Sweep
# kaputt" und „Board stark benutzt" unterscheiden — er stand danach weiter rot, obwohl der
# Sweep gesund war, und wäre damit zu Rauschen geworden. Der Heartbeat misst stattdessen
# direkt, was zählt: den letzten erfolgreichen Lauf. Gitignoriert (journal/), Mac-lokal —
# hier läuft der nightly evening-sync. Auslesen: context-health-check.py Guard „sweep alive".
HEARTBEAT = Path(__file__).resolve().parent / "journal" / "sweep-heartbeat.json"


def write_heartbeat(swept: int, closed: int, path: Path | None = None) -> None:
    """Nach jedem Lauf, der SAUBER durchlief — auch wenn es nichts zu tun gab („nichts zu
    archivieren" ist ein gesunder Sweep, kein Ausfall). NICHT nach dem lost_total-Abbruch
    und nicht im Dry-Run. Fehler beim Schreiben werden geschluckt: ein kaputter Heartbeat
    darf niemals den Sweep verhindern — das wäre exakt der Fehler, den dieser Fix behebt."""
    p = path or HEARTBEAT
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "swept": swept, "closed": closed,
        }) + "\n")
    except OSError as e:
        print(f"sweep: Heartbeat nicht geschrieben ({e}) — Lauf selbst war erfolgreich")


def open_thread(it: dict) -> bool:
    """Faden offen = es gibt Turns und der letzte ist kein @gc-done. Solche Items
    NICHT sweepen — sonst verschwindet ein noch laufender Austausch aus dem Board."""
    t = it.get("thread", [])
    return bool(t) and t[-1]["kind"] != "done"


AUTO_DONE_NOTE = "(auto: Item abgehakt — Sweep)"

# Datenformat, nicht UI — Definition in markers.py (ohne Zeilenende-Anker: hier reicht
# "kommt der Pfad vor", weil auf den Archivpfad umgeschrieben wird).
from markers import SIDECAR_REF_RE  # noqa: E402,F401


def sidecar_files_for(gc_id: str, sidecar_dir: Path = SIDECAR_DIR) -> list[Path]:
    """Alle Sidecar-Dateien eines Items — Schema `<gc-id>-<timestamp>-<suffix>.md`
    (`sidecar.write_sidecar`); der gc-id-Präfix identifiziert sie eindeutig."""
    if not gc_id or not sidecar_dir.is_dir():
        return []
    return sorted(sidecar_dir.glob(f"{gc_id}-*.md"))


def archive_sidecars(it: dict, sidecar_dir: Path = SIDECAR_DIR,
                      archive_dir: Path = SIDECAR_ARCHIVE_DIR
                      ) -> tuple[list[tuple[Path, Path]], dict[str, str], list[str]]:
    """Sidecar-Umzug nach gc-threads/archive/ VORMERKEN — bewegt noch NICHTS. Reihenfolge-
    Fix (2026-07-21 Review): würden wir hier schon verschieben, wären die Sidecars weg,
    bevor board.md/board-archive.md geschrieben sind — ein Crash dazwischen ließe Board-Zeile
    und Faden-Verweise auf ins Leere zeigende Dateien zurück. Stattdessen: Kollisionscheck
    (Zieldatei existiert schon → überspringen + Warnung, NIE überschreiben) läuft SOFORT, damit
    die Ziel-Map fürs Text-Rewrite feststeht; die eigentlichen shutil.move-Aufrufe führt
    apply_sidecar_moves() aus — vom Aufrufer NACH erfolgreichem Board-Write.
    → (Move-Plan [(Quelle, Ziel)], alter Dateiname -> neuer Verweis-Pfad, Warnungen)."""
    plan: list[tuple[Path, Path]] = []
    moved: dict[str, str] = {}
    warnings: list[str] = []
    for f in sidecar_files_for(it.get("id", ""), sidecar_dir):
        dest = archive_dir / f.name
        if dest.exists():
            warnings.append(f"sweep: Sidecar-Kollision — {f.name} liegt schon in "
                             "gc-threads/archive/, übersprungen (Original bleibt liegen)")
            continue
        plan.append((f, dest))
        moved[f.name] = f"inbox/gc-threads/archive/{f.name}"
    return plan, moved, warnings


def apply_sidecar_moves(plan: list[tuple[Path, Path]], archive_dir: Path = SIDECAR_ARCHIVE_DIR) -> None:
    """Move-Plan aus archive_sidecars() tatsächlich ausführen — vom Aufrufer erst NACH
    erfolgreichem board-archive.md/board.md-Write aufgerufen (siehe _run), damit ein Crash
    beim Board-Schreiben niemals Sidecars ohne zugehörige Archiv-Zeile hinterlässt.
    Defensive Re-Prüfung (Quelle noch da, Ziel noch frei): der Kollisionscheck lief schon
    beim Planen, aber zwischen Plan und Ausführung kann sich das Verzeichnis ändern."""
    if not plan:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    for src, dest in plan:
        if src.exists() and not dest.exists():
            shutil.move(str(src), str(dest))


def rewrite_sidecar_refs(text: str, moved: dict[str, str]) -> str:
    """Pfad-Referenzen im archivierten Textblock auf den neuen Sidecar-Ort umschreiben.
    Ersetzt NUR Dateinamen, die tatsächlich verschoben wurden (moved-Map) — ein Verweis
    auf eine fremde/nicht verschobene Datei (z.B. Kollision übersprungen) bleibt unberührt."""
    if not moved:
        return text
    return SIDECAR_REF_RE.sub(lambda m: moved.get(m.group(1), m.group(0)), text)


def archive_completed_theme(
    theme_name: str,
    closer_id: str,
    board_path: Path = BOARD,
    archive_path: Path = ARCHIVE,
    sidecar_dir: Path = SIDECAR_DIR,
    sidecar_archive_dir: Path = SIDECAR_ARCHIVE_DIR,
) -> tuple[bool, str, int]:
    """Archive one fully completed topic immediately and remove it from the board.

    The first-run closer uses this instead of deleting its own row from inside an agent
    run: that would remove the item before the runner can append its final reply. The UI
    closes the thread first, then calls this helper through the server. All item blocks
    reach ``board-archive.md`` before the active topic disappears, and sidecars move only
    after both markdown files are safely written.
    """
    with board_write_guard(board_path):
        raw = board_path.read_text(encoding="utf-8")
        board = parse_board(raw)
        if lost_total(raw, board) > 0:
            return False, "The board has unparsed lines — onboarding cleanup is blocked", 0
        theme = next((t for t in board["themes"] if t["name"] == theme_name), None)
        if theme is None:
            return True, "Getting started is already closed", 0
        located = [(col, it) for col in theme_cols(theme) for it in theme["cols"][col]]
        closer = next((it for _col, it in located if it.get("id") == closer_id), None)
        if closer is None or closer.get("title") != "Finish Getting started":
            return False, "The closing to-do was not found in Getting started", 0
        open_titles = [it["title"] for _col, it in located if not it.get("done")]
        if open_titles:
            return False, "Complete or skip first: " + ", ".join(open_titles), 0

        moves: list[tuple[Path, Path]] = []
        blocks: list[str] = []
        for col, it in located:
            plan, moved, warnings = archive_sidecars(it, sidecar_dir, sidecar_archive_dir)
            if warnings:
                return False, warnings[0], 0
            moves.extend(plan)
            blocks.append(rewrite_sidecar_refs(fmt_item(it, f"{theme_name} / {col}"), moved))

        existing = archive_path.read_text(encoding="utf-8") if archive_path.is_file() else ""
        unseen = [block for block, (_col, it) in zip(blocks, located)
                  if not it.get("id") or it["id"] not in existing]
        if unseen:
            header = ("" if existing else
                      "# Board archive\n\nCompleted to-dos preserved by Superboard.\n")
            addition = f"{header}\n## {date.today().isoformat()}\n\n" + "\n\n".join(unseen) + "\n"
            tmp_archive = archive_path.with_name(".board-onboarding-archive.tmp")
            tmp_archive.write_text(existing + addition, encoding="utf-8")
            tmp_archive.replace(archive_path)

        board["themes"] = [t for t in board["themes"] if t is not theme]
        tmp_board = board_path.with_name(".board-onboarding-close.tmp")
        tmp_board.write_text(serialize_board(board), encoding="utf-8")
        tmp_board.replace(board_path)
        apply_sidecar_moves(moves, sidecar_archive_dir)
        return True, "Getting started archived", len(located)


def close_done_threads(board: dict) -> list[str]:
    """Archiv-Deadlock-Fix (2026-07-16, Q3): Abhaken = schließbar. Abgehakte Items
    mit offenem Faden bekommen einen @gc-done:-Turn angehängt (gleiche Zeilen-Syntax
    wie das Board-UI, GC_DONE_RE in server.py verträgt Text nach dem Doppelpunkt).
    Läuft VOR der Archiv-Prüfung — reife Items gehen im selben Lauf mit raus.
    → Liste „Ort: Titel" fürs Log."""
    closed: list[str] = []
    spots = [(f"{t['name']} / {c}", it)
             for t in board["themes"] for c in theme_cols(t) for it in t["cols"][c]]
    spots += [(f"Person: {p['name']}", it) for p in board["persons"] for it in p["items"]]
    for origin, it in spots:
        if it["done"] and open_thread(it):
            it["thread"].append({"kind": "done", "text": AUTO_DONE_NOTE})
            closed.append(f"{origin}: {it['title']}")
    return closed


def is_chat_card(it: dict) -> bool:
    """Tages-Chat-Karte des Cockpits — erkannt am `chat:JJJJ-MM-TT`-Marker im Body,
    den server._chat_send setzt. Bewusst NICHT am Titel („Chat 2026-08-23"): der Titel
    ist Anzeige, der Marker ist Datenformat."""
    return any(CHAT_MARKER_RE.match(b.strip()) for b in it.get("body", []))


def last_activity(it: dict) -> datetime | None:
    """Wann war zuletzt etwas los? Primaer der @gc-last-Stempel (LOKALZEIT), sonst None -
    der Aufrufer faellt dann auf das Item-Datum zurueck."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", it.get("gc_last", ""))
    if not m:
        return None
    try:
        return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}:{m.group(3)}")
    except ValueError:
        return None


def retire_chat_cards(board: dict, now: datetime | None = None) -> list[str]:
    """Fuenfter Job: ruhende Cockpit-Chat-Karten abhaken (2026-08-25).

    Laeuft VOR close_done_threads - der schliesst den dann noch offenen Faden mit einem
    @gc-done:, sonst hielte open_thread() die Karte fuer immer im Board. Der eigentliche
    Umzug ins Archiv passiert weiter unten im normalen Pfad, nur mit eigener Frist
    (CHAT_IDLE_HOURS statt RETENTION_HOURS, siehe _run).

    Ruhe-Anker: @gc-last, sonst das Ende des Chat-Tages. Der Fallback ist bewusst
    grosszuegig - eine Karte von heute ohne jede Aktivitaet bleibt damit liegen, statt
    mitten am Tag wegzuraeumen, waehrend sie vielleicht gleich weiterbenutzt wird.
    -> Liste "Titel (ruht seit ...)" fuers Log."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=CHAT_IDLE_HOURS)
    retired: list[str] = []
    for it in board.get("cockpit", []):
        if it["done"] or not is_chat_card(it):
            continue
        idle_since = last_activity(it)
        if idle_since is None:
            try:
                idle_since = datetime.combine(date.fromisoformat(it.get("date", "")),
                                              datetime.max.time()).replace(microsecond=0)
            except ValueError:
                continue
        if idle_since > cutoff:
            continue
        it["done"] = True
        # Faden gleich mit schliessen. close_done_threads() greift NUR auf Themen- und
        # Personen-Items zu (die Cockpit-Sektion sieht es nicht) - ohne diese Zeile bliebe
        # die Karte ueber open_thread() fuer immer im Board haengen, abgehakt und unarchiviert.
        if open_thread(it):
            it["thread"].append({"kind": "done", "text": AUTO_DONE_NOTE})
        # done_at treibt unten die Reifepruefung - in UTC, wie ueberall im Board. Der
        # Stempel ist der Ruhe-Zeitpunkt, nicht "jetzt": sonst waere die Karte im selben
        # Lauf abgehakt, aber erst 3h spaeter reif, und der Sweep braeuchte zwei Naechte.
        it["done_at"] = idle_since.astimezone(timezone.utc).isoformat(timespec="seconds")
        hours = round((now - idle_since).total_seconds() / 3600)
        retired.append(f"{it['title']} (ruht seit {hours}h)")
    return retired


def fmt_item(it: dict, origin: str) -> str:
    lines = [f"- [x] {it['title']} *({it['date']})* ← {origin}"]
    lines += [f"  {b}" for b in it.get("body", [])]
    if it.get("id"):
        lines.append(f"  @gc-id: {it['id']}")
    if it.get("wait") or it.get("wait_since"):
        since = f"*({it['wait_since']})*" if it.get("wait_since") else ""
        lines.append("  @wait: " + " ".join(p for p in (it.get("wait", ""), since) if p))
    if it.get("done_at"):
        lines.append(f"  @done-at: {it['done_at']}")
    lines += [f"  {gc_tag(e['kind'])} {e.get('text', '')}".rstrip() for e in it.get("thread", [])]
    if it.get("session"):
        lines.append(f"  @gc-session: {it['session']}")
    if it.get("sessions"):
        lines.append(f"  @gc-sessions: {', '.join(it['sessions'])}")
    lines += [f"  - [{'x' if s['done'] else ' '}] {s['text']}" for s in it.get("subs", [])]
    return "\n".join(lines)


def stamp_missing_done_at(board: dict, now: datetime | None = None) -> list[str]:
    """Retentions-Deadlock (Board-Maintenance 04.09.): ein abgehaktes Item OHNE
    `@done-at` UND ohne `*(Datum)*` liefert in done_at() None — ripe() ist damit
    dauerhaft False und die Karte bleibt fuer immer abgehakt in einer aktiven Spalte
    stehen (gefunden: die vierkoepfige Familie „Growth strategy", Team & Org / Jetzt,
    seit Ende Juli). Der Sweep stempelt hier `@done-at` auf JETZT: nichts verschwindet
    sofort, die normale 25h-Schonfrist beginnt einfach zu laufen. Nur der Fall ohne
    beide Zeitquellen — ein vorhandenes `date` bleibt der grosszuegige Fallback.
    → Liste „Ort: Titel" fuers Log."""
    ts = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    stamped: list[str] = []
    spots = [(f"{t['name']} / {c}", it)
             for t in board["themes"] for c in theme_cols(t) for it in t["cols"][c]]
    spots += [(f"Person: {p['name']}", it) for p in board["persons"] for it in p["items"]]
    for origin, it in spots:
        if it["done"] and not it.get("done_at") and not it.get("date"):
            it["done_at"] = ts
            stamped.append(f"{origin}: {it['title']}")
    return stamped


def done_at(it: dict) -> datetime | None:
    """Effektiver Abhak-Zeitpunkt für die Stunden-Retention: @done-at (UTC-ISO) wenn
    vorhanden, sonst Tagesende (23:59:59 UTC) des `date`-Felds als großzügiger Fallback
    für Items ohne Stempel (alte Einträge, Hand-Edits) — die sollen nicht plötzlich
    verschwinden, nur weil ihnen der neue Stempel fehlt."""
    ts = it.get("done_at", "")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    d = it.get("date", "")
    if not d:
        return None
    try:
        return datetime.combine(date.fromisoformat(d), datetime.max.time()).replace(
            microsecond=0, tzinfo=timezone.utc)
    except ValueError:
        return None


def escalate_waits(board: dict, today: date) -> tuple[list[str], int, bool]:
    """Wait-Verfall = ESKALATION AN ORT UND STELLE, nicht Rückholung (2026-07-22).

    Vorher schob der Sweep unbestätigte Waits nach WAIT_DECAY_DAYS zurück nach „Jetzt",
    stempelte einen ⚠-Vermerk in den Body und löschte `wait`/`wait_since`. Das zerstörte
    genau die Information, die den Nachfass-Impuls trägt: WORAUF wartet es und SEIT WANN.
    Übrig blieb ein kontextloses Item in einer ohnehin vollen Spalte — und weil das Wait
    weg war, war auch nicht mehr sichtbar, dass jemand anderes am Zug ist.

    Neu: Das Item BLEIBT in „Wartet auf andere", behält Referenz und Datum, und wandert
    nur an den Anfang der Spalte. Überfällig-Sein ist ein Zustand, den das Badge rot
    anzeigt („⏳ slim · !475 · überfällig 8d") — kein Umzug. Damit liest sich die Spalte
    als Nachfass-Radar: oben steht, wo Feedback überfällig ist, darunter das Frische.
    Der Friedhofs-Schutz ist nicht mehr der Rauswurf, sondern die Sichtbarkeit (Badge rot,
    Cockpit-Kachel „überfällig", Attention-Zeile beim Namen).

    Anker ist AUSSCHLIESSLICH wait_since (= wann der Wait gesetzt/zuletzt bestätigt wurde),
    NICHT das Item-Datum: das ist das Erstelldatum, ein altes Item mit frischem @wait wäre
    sonst sofort überfällig. Wait ohne Stempel (Hand-Edit) → heute stempeln, Uhr läuft ab jetzt.

    → (überfällige Items fürs Log, Anzahl Stempel, ob sich etwas GEÄNDERT hat).
    Das dritte Feld ist nötig, weil überfällige Waits liegenbleiben: ohne es würde der
    Sweep board.md ab jetzt jede Nacht neu schreiben, obwohl sich nichts bewegt hat."""
    cutoff = (today - timedelta(days=WAIT_DECAY_DAYS)).isoformat()
    overdue: list[str] = []
    stamped = 0
    changed = False
    for theme in board["themes"]:
        if WAIT_COL not in theme["cols"]:
            continue
        items = theme["cols"][WAIT_COL]
        for it in items:
            if it["done"]:
                continue
            if not it.get("wait_since"):
                it["wait_since"] = today.isoformat()
                stamped += 1
                changed = True
        def is_overdue(it: dict) -> bool:
            anchor = it.get("wait_since", "")
            return bool(not it["done"] and anchor and anchor <= cutoff)
        for it in items:
            if is_overdue(it):
                days = (today - date.fromisoformat(it["wait_since"])).days
                label = it.get("wait") or "ohne Referenz"
                overdue.append(f"{theme['name']}: {it['title']} (wartet auf {label}, überfällig seit {days}d)")
        # Überfällige nach oben, Reihenfolge innerhalb der Gruppen bleibt stabil (sorted ist stable).
        reordered = sorted(items, key=lambda it: not is_overdue(it))
        if [id(x) for x in reordered] != [id(x) for x in items]:
            changed = True  # Identität vergleichen, nicht Inhalt — zwei Items können gleich aussehen
        theme["cols"][WAIT_COL] = reordered
    return overdue, stamped, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.dry_run:
        return _run(dry_run=True)
    # P0-Fix (ext. Review GPT-5.6, 2026-07-16): sweep schrieb board.md als eigener Prozess
    # UNGESCHÜTZT am Server-Lock vorbei — ein paralleler gc-append zwischen read und write
    # ging still verloren. Jetzt derselbe Interprozess-Guard wie alle Server-Schreibpfade;
    # der Sweep ist schnell (reine File-Ops), der Server wartet schlimmstenfalls <1 s.
    with board_write_guard(BOARD):
        return _run(dry_run=False)


def _run(dry_run: bool) -> int:
    text = BOARD.read_text()
    board = parse_board(text)
    if lost_total(text, board) > 0:
        print("sweep: ABBRUCH — board.md hat ungeparste Zeilen (Checkbox/@gc/Session/ID), nichts angefasst")
        return 1

    # Ruhende Cockpit-Chat-Karten abhaken, BEVOR die Faeden geschlossen werden - sonst
    # hielte ihr offener Faden sie ueber open_thread() dauerhaft im Board.
    retired = retire_chat_cards(board)

    # Deadlock-Auflösung VOR der Archiv-Prüfung: abgehakte Items mit offenem Faden
    # schließen, damit open_thread() sie unten nicht mehr vor dem Archiv schützt.
    closed = close_done_threads(board)

    # Ohne @done-at UND ohne date bleibt ripe() fuer immer False — Stempel nachziehen,
    # damit die Schonfrist ueberhaupt anfaengt zu laufen (statt Dauerbestand im Board).
    dated = stamp_missing_done_at(board)

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=RETENTION_HOURS)
    # Chat-Karten haben eine eigene, viel kuerzere Frist: die 25h-Schonfrist existiert fuer
    # den Morgen-Rueckblick auf erledigte ARBEIT - ein Chat-Protokoll steht dort nie.
    chat_cutoff = now_utc - timedelta(hours=CHAT_IDLE_HOURS)
    swept: list[str] = []
    sidecar_warnings: list[str] = []
    sidecar_moves: list[tuple[Path, Path]] = []

    def ripe(it: dict) -> bool:
        ts = done_at(it)
        limit = chat_cutoff if is_chat_card(it) else cutoff
        return bool(it["done"] and ts and ts <= limit and not open_thread(it))

    # Eltern und Subs nur GEMEINSAM archivieren (Sol-Befund 2): archivierte der Sweep sie
    # unabhängig, blieben verwaiste `@gc-parent`-Zeiger zurück — Nachladen, Roll-up-Ziel und
    # der „Subs 1/3"-Zähler zeigen dann auf ein Item, das im aktiven Board nicht mehr
    # existiert. Regel: eine Familie ist erst reif, wenn ALLE ihre Mitglieder reif sind.
    idx = item_index(board)
    held: set[int] = set()   # id() der Items, die auf ihre Familie warten
    for _s, _n, _c, it in _all_items(board):
        if it.get("parent") or not it.get("id"):
            continue
        kids = children_of(board, it["id"], idx)
        if not kids:
            continue
        family = [it, *kids]
        if not all(ripe(m) for m in family):
            held.update(id(m) for m in family if ripe(m))

    def is_stale(it: dict) -> bool:
        return ripe(it) and id(it) not in held

    def archive_item(it: dict, origin: str) -> str:
        """Faden-Retention: Sidecar-Umzug nur VORMERKEN (sidecar_moves sammelt den Plan),
        Text schon mit den neuen Pfaden formatieren. Die eigentlichen Moves laufen erst
        ganz am Ende von _run(), NACHDEM board-archive.md/board.md sicher geschrieben sind.
        SIDECAR_DIR/SIDECAR_ARCHIVE_DIR explizit als Modul-Globals durchgereicht (nicht über
        Default-Parameter) — Tests biegen `sweep.SIDECAR_DIR` um, das wirkt nur bei einem
        Namelookup zur Laufzeit, nicht auf ein beim Funktionsdefinieren gebundenes Default."""
        plan, moved, warns = archive_sidecars(it, SIDECAR_DIR, SIDECAR_ARCHIVE_DIR)
        sidecar_moves.extend(plan)
        sidecar_warnings.extend(warns)
        return rewrite_sidecar_refs(fmt_item(it, origin), moved)

    for theme in board["themes"]:
        # theme_cols statt der 3 Default-Spalten — sonst würden done-Items in
        # „Wartet auf andere" nie archiviert (Bug bis 2026-07-14).
        for col in theme_cols(theme):
            keep = []
            for it in theme["cols"][col]:
                if is_stale(it):
                    swept.append(archive_item(it, f"{theme['name']} / {col}"))
                else:
                    keep.append(it)
            theme["cols"][col] = keep
    for p in board["persons"]:
        keep = []
        for it in p["items"]:
            if is_stale(it):
                swept.append(archive_item(it, f"Person: {p['name']}"))
            else:
                keep.append(it)
        p["items"] = keep
    # Cockpit: BEWUSST nur Chat-Karten. Die uebrigen Cockpit-Eintraege sind Aktions-Karten
    # - Dauer-Items, die nie archiviert werden duerfen, auch wenn irgendwann jemand
    # versehentlich einen Haken setzt.
    keep = []
    for it in board.get("cockpit", []):
        if is_chat_card(it) and is_stale(it):
            swept.append(archive_item(it, "Cockpit"))
        else:
            keep.append(it)
    board["cockpit"] = keep

    overdue, stamped, waits_changed = escalate_waits(board, date.today())

    # `overdue` gehört ins LOG, gatet aber NICHT den Write: überfällige Waits bleiben
    # jetzt liegen, der Sweep würde board.md sonst jede Nacht ohne Änderung neu schreiben.
    if not swept and not waits_changed and not closed and not retired:
        msg = "sweep: nichts zu archivieren, keine Wait-Änderung"
        print(msg + (f" · {len(overdue)} Wait(s) überfällig — nachfassen" if overdue else ""))
        # Heartbeat AUCH hier: „nichts zu tun" ist ein gesunder Lauf. Ohne diese Zeile
        # sähe ein ruhiges Board aus wie ein toter Sweep — der Guard würde falsch anschlagen.
        if not dry_run:
            write_heartbeat(0, 0)
        return 0
    if dry_run:
        if retired:
            print(f"sweep (dry-run): würde {len(retired)} Cockpit-Chat-Karte(n) stilllegen:")
            print("\n".join(f"- {r}" for r in retired))
        if closed:
            print(f"sweep (dry-run): würde {len(closed)} offene(n) Faden abgehakter Items schließen (@gc-done):")
            print("\n".join(f"- {c}" for c in closed))
        if swept:
            print(f"sweep (dry-run): würde {len(swept)} Item(s) archivieren:")
            print("\n".join(swept))
        if overdue:
            print(f"sweep (dry-run): {len(overdue)} Wait(s) überfällig (bleiben in „{WAIT_COL}“, nach oben sortiert):")
            print("\n".join(f"- {d}" for d in overdue))
        if stamped:
            print(f"sweep (dry-run): würde {stamped} undatierte Wait(s) mit heute stempeln")
        if dated:
            print(f"sweep (dry-run): würde {len(dated)} abgehakte(n) Item(s) ohne jeden "
                  "Zeitstempel mit @done-at=jetzt versehen (sonst nie archivierbar):")
            print("\n".join(f"- {d}" for d in dated))
        if sidecar_warnings:
            print(f"sweep (dry-run): {len(sidecar_warnings)} Sidecar-Warnung(en):")
            print("\n".join(f"- {w}" for w in sidecar_warnings))
        _warn_unknown_kinds()
        return 0

    if swept:
        header = "" if ARCHIVE.exists() else f"# Board-Archiv\n\nVon sweep.py automatisch archivierte erledigte Items (done > {RETENTION_HOURS}h).\n"
        with ARCHIVE.open("a") as f:
            f.write(f"{header}\n## {date.today().isoformat()}\n\n" + "\n\n".join(swept) + "\n")
    # Atomar (temp+rename) wie jeder Server-Write — ein Crash mitten im write_text
    # hätte board.md halb geschrieben zurückgelassen.
    tmp = BOARD.with_name(".board-sweep.tmp")
    tmp.write_text(serialize_board(board))
    tmp.replace(BOARD)
    # Sidecars ERST JETZT tatsächlich verschieben — board-archive.md ist geschrieben,
    # board.md ist ersetzt. Reihenfolge-Fix (2026-07-21 Review): ein Crash vor diesem
    # Punkt hinterlässt bestenfalls unverschobene Sidecars (Original bleibt einfach liegen,
    # nichts geht verloren), statt Sidecars ohne zugehörige Board-/Archiv-Zeile.
    apply_sidecar_moves(sidecar_moves, SIDECAR_ARCHIVE_DIR)
    print(f"sweep: {len(swept)} Item(s) → {ARCHIVE.name}"
          + (f" · {len(overdue)} Wait(s) überfällig — nachfassen" if overdue else "")
          + (f" · {len(closed)} Faden abgehakter Items geschlossen" if closed else "")
          + (f" · {len(retired)} Cockpit-Chat-Karte(n) stillgelegt" if retired else "")
          + (f" · {len(dated)} Item(s) ohne Zeitstempel mit @done-at nachgestempelt" if dated else ""))
    if sidecar_warnings:
        print(f"sweep: {len(sidecar_warnings)} Sidecar-Warnung(en):")
        print("\n".join(f"- {w}" for w in sidecar_warnings))
    _warn_unknown_kinds()
    write_heartbeat(len(swept), len(closed))
    return 0


def _warn_unknown_kinds() -> None:
    """Unbekannte Faden-Kinds sind kein Abbruchgrund mehr, aber auch nichts, was still
    bleiben darf: die Zeile geht in die Sweep-Ausgabe und damit in den Morgen-Digest."""
    if UNKNOWN_KINDS:
        print("sweep: WARNUNG — unbekannte Faden-Kind(s) generisch serialisiert: "
              + ", ".join(sorted(UNKNOWN_KINDS))
              + " (server.GC_TAG ergänzen, sonst liest der Parser sie nicht zurück)")


if __name__ == "__main__":
    sys.exit(main())
