#!/usr/bin/env python3
"""Sagt in zwei Sekunden, WARUM das Board gesperrt ist — und welche Zeile schuld ist.

## Der Anlass (2026-07-28)

Der Server hat einen Schutz: `lost_total(text, board) > 0` heißt, der Parser hat
Roh-Zeilen in board.md nicht wiedererkannt. Ein `serialize_board()`-Save würde sie
still vernichten. Deshalb blockt JEDER Schreibpfad mit HTTP 409 — neues Item,
Agent-Run, Faden-Antwort, Chat, Capture.

Der Schutz ist richtig. Kaputt war die Diagnose: acht Minuten lang stand `lost` auf
10, das Board nahm nichts mehr an, und weder ein Mensch noch ein Board-Run konnte
sehen, WELCHE Zeilen gemeint waren. Es sah aus wie „das Board ist kaputt".
Drei fertige Agent-Antworten strandeten im Journal (die Journal-Wache hat sie später
selbst nachgetragen — kein Datenverlust, aber acht Minuten Blindflug).

## Warum Round-Trip statt neuer Zähler

Die `lost_*`-Guards im Server ZÄHLEN nur (Regex-Treffer im Rohtext minus geparste
Felder) — sie wissen nicht, welche Zeile fehlt. Hier wird stattdessen genau die
Operation nachgestellt, vor der der Guard schützt:

    parse_board(text) -> serialize_board(board) -> zurückvergleichen

Was im Original steht und im Round-Trip fehlt, IST die Menge der Zeilen, die ein Save
vernichten würde. Kein Modell davon, sondern die Sache selbst.

Gemessen (28.07., board.md mit 163 Items / 493 Faden-Turns): Der Round-Trip ist auf
einem sauberen Board bit-genau — 0 verlorene, 0 neue Zeilen. Gegen drei künstlich
gebaute Defekte (doppelte `@gc-id`, verwaiste `@gc:`-Zeile, falsch eingerückte
Checkbox) trifft er den Server-Guard exakt 1:1 und benennt jeweils die Zeile.
Diese Bit-Genauigkeit ist die Voraussetzung des Verfahrens: bricht sie (weil
serialize_board bewusst normalisiert), meldet dieses Skript Fehlalarme — dann gehört
die Normalisierung in `_norm()` nachgezogen, nicht der Befund weggedrückt.

## Zweiter Befundtyp: Struktur-Doppel (2026-08-21)

Der Round-Trip findet nur, was ein Save VERNICHTEN würde. Er ist blind für den
umgekehrten Schaden: Zeilen, die es doppelt gibt. Ein Splice-Unfall (End-Anker vor
Start-Anker) kann Items und eine ganze `##`-Überschrift verdoppeln — `lost` bleibt
dabei 0, weil doppelte Items syntaktisch völlig legal sind. Das Board zeigt dann
Geisterkarten, und Schreibpfade landen mal in der einen, mal in der anderen Kopie.

Deshalb prüft `lint()` zusätzlich zwei Eindeutigkeits-Invarianten:

* **`dup_ids`** — dieselbe `@gc-id` an mehr als einem Item. Gezählt wird über die
  GEPARSTEN Items, nicht per Textsuche: eine `@gc-id` im Fließtext eines Bodies
  (Cross-Ref auf ein anderes Item) ist erlaubt und darf nicht mitzählen.
* **`dup_themes`** — derselbe Themenname an mehr als einer `##`-Überschrift.

Beide sperren das Board NICHT (`locked` hängt weiter allein am Round-Trip): ein
Doppel ist ein Datenfehler, kein Grund, dem Menschen das Schreiben zu verbieten. Sie
setzen aber den Exit-Code auf 1 und stehen im Report — genau die Sichtbarkeit, die
sonst fehlen würde.

## Nutzung

    python3 -m superboard.board_lint            # Klartext, Exit 1 wenn gesperrt/doppelt
    python3 -m superboard.board_lint --json     # für Skripte/Agenten
    python3 -m superboard.board_lint <pfad>     # anderes Board

Als Modul: `from board_lint import lint; lint(text)` -> dict.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _p  # noqa: E402

HERE = _p.HERE
DEFAULT_BOARD = _p.BOARD

# Was die verlorene Zeile für den Menschen bedeutet. Reihenfolge = Prüfreihenfolge,
# erster Treffer gewinnt; der Fallback steht unten in _hint().
HINTS: list[tuple[str, str]] = [
    (r"^\s*@gc-id:", "Item-ID. Meist: zwei @gc-id-Zeilen an einem Item — eine wird still "
                     "verworfen, und damit adressieren Runs das falsche Item."),
    (r"^\s*@gc-session:", "Resume-Pointer der Agent-Session. Verloren = der nächste Run "
                          "startet ohne Vorgeschichte statt fortzusetzen."),
    (r"^\s*@gc-sessions:", "Verlaufsliste abgelöster Session-UUIDs (Rückblätter-Historie "
                           "nach einem Kontext-Schnitt). Verloren = alte Sessions sind aus "
                           "Board-Sicht nicht mehr auffindbar (Transkripte liegen weiter auf Platte)."),
    (r"^\s*@gc-parent:", "Kante zum Eltern-Item (Sub-Faden). Verloren = die Hierarchie reißt."),
    (r"^\s*@gc-last:", "Run-Meta (Tokens/Kosten/Zeit) des letzten Laufs."),
    (r"^\s*@gc(-re|-done)?:", "Faden-Turn. Verloren = ein Gesprächsbeitrag verschwindet aus "
                             "dem Faden — das ist der teuerste Fall."),
    (r"^\s*@wait:", "Warte-Feld (Waiting-for-others)."),
    (r"^\s*@on:", "Stichtag des Termin-To-dos."),
    (r"^\s*@done-at:", "Erledigt-Stempel."),
    (r"^\s*@stage:", "Prozess-Stufe (plan/rfc/wip/...)."),
    (r"^\s*- \[[ xX]\]", "Checkbox-Zeile, die der Parser nicht als Item/Sub gelesen hat — "
                         "fast immer falsche Einrückung oder ein Item außerhalb einer Spalte."),
]

ITEM_RE = re.compile(r"^\s*- \[[ xX]\]\s*(.*)$")


def _load_server():
    """server.py als Modul laden. sys.path-Eintrag, weil server.py `import sidecar`
    macht — ohne den Pfad stirbt der Import an einem Nachbarmodul, nicht an sich selbst."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("_board_server", HERE / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canon_heading(line: str, server) -> str:
    """Eine `### `/`# `-Überschriftszeile auf EINE Schreibweise normalisieren, bevor sie
    in den Verlust-Diff geht — sonst sähe server.py's Datei-Grenz-Übersetzung (parse
    akzeptiert Deutsch UND Englisch, serialize schreibt immer Englisch) wie eine
    vernichtete Zeile aus: ein sauberes Alt-Board mit `### Jetzt` würde nach dem
    Round-Trip `### Now` heißen, und `_norm` hielte das für Verlust.
    Kein Server-Modul übergeben (ältere Aufrufer) → Zeile bleibt unverändert, wie vorher."""
    if server is None:
        return line
    if line.startswith("### "):
        key = server.column_key(line[4:].strip())
        return f"### {key}" if key else line
    if line.startswith("# "):
        key = server.section_key(line[2:].strip())
        return f"# {key}" if key else line
    return line


def _norm(text: str, server=None) -> Counter:
    """Vergleichsform: leere Zeilen raus, beidseitig trimmen, Überschriften auf eine
    Schreibweise normalisiert. Multiset statt Liste — verschobene Zeilen sind kein
    Verlust, nur fehlende sind einer.

    Warum auch LINKS getrimmt wird (Test `test_lint_und_guard_sind_sich_einig`,
    Fehlalarm 28.07.): `serialize_board` schreibt mit eigener, kanonischer Einrückung.
    Eine mit drei Leerzeichen eingerückte Checkbox liest der Parser als Sub-Item und
    gibt sie mit zwei zurück — inhaltlich nichts verloren, der Guard sagt korrekt 0.
    Mit rstrip-Vergleich sah das aus wie eine vernichtete Zeile. Umformatierung ist
    kein Verlust; nur verschwundener INHALT ist einer."""
    return Counter(_canon_heading(line.strip(), server)
                   for line in text.split("\n") if line.strip())


def _hint(line: str) -> str:
    for pattern, text in HINTS:
        if re.match(pattern, line):
            return text
    return "Zeile gehört zu keinem geparsten Feld."


def _owner(lines: list[str], idx: int) -> str:
    """Titel des Items, unter dem die Zeile hängt (rückwärts bis zur nächsten Checkbox).
    Ohne das ist eine `@gc-id:`-Zeile ohne Kontext kaum zuzuordnen.

    Start bei idx-1, nicht idx: ist die verlorene Zeile SELBST eine Checkbox, wäre sie
    sonst ihr eigener Besitzer ("am Item: falsch eingerückte Box") — der Nutzer will
    aber wissen, wo im Board sie hängt."""
    for k in range(idx - 1, -1, -1):
        if m := ITEM_RE.match(lines[k]):
            return m.group(1).strip()[:80]
    return "(kein Item darüber — Zeile hängt frei)"


def _duplicates(text: str, board: dict, m) -> tuple[list[dict], list[dict]]:
    """Eindeutigkeits-Invarianten: eine @gc-id je Item, ein Themenname je Überschrift.

    Quelle sind die GEPARSTEN Items — eine `@gc-id:` im Fließtext eines Bodies ist
    ein legitimer Cross-Ref und zählt bewusst nicht mit (sonst meldet der Lint auf
    dem echten Board ein Doppel, das keines ist)."""
    raw = text.split("\n")
    seen: dict[str, list[dict]] = {}
    for _sec, _name, _col, it in m._all_items(board):
        gid = (it.get("id") or "").strip()
        if gid:
            seen.setdefault(gid, []).append(it)

    dup_ids = []
    for gid, hits in seen.items():
        if len(hits) < 2:
            continue
        lines = [i + 1 for i, line in enumerate(raw)
                 if line.strip() == f"@gc-id: {gid}"]
        dup_ids.append({"id": gid, "count": len(hits), "lines": lines,
                        "titles": [h.get("title", "")[:70] for h in hits]})
    dup_ids.sort(key=lambda d: d["lines"][0] if d["lines"] else 0)

    names: dict[str, int] = {}
    for th in board.get("themes", []):
        names[th["name"]] = names.get(th["name"], 0) + 1
    dup_themes = []
    for name, count in names.items():
        if count < 2:
            continue
        lines = [i + 1 for i, line in enumerate(raw) if line.strip() == f"## {name}"]
        dup_themes.append({"name": name, "count": count, "lines": lines})
    dup_themes.sort(key=lambda d: d["lines"][0] if d["lines"] else 0)
    return dup_ids, dup_themes


def lint(text: str, server=None) -> dict:
    """{'locked': bool, 'lost': int, 'lines': [...], 'items', 'thread', 'dup_ids', 'dup_themes'}"""
    m = server or _load_server()
    board = m.parse_board(text)
    missing = _norm(text, m) - _norm(m.serialize_board(board), m)

    raw_lines = text.split("\n")
    found = []
    for content, count in missing.items():
        # .strip() muss zu _norm() passen — sonst findet die Rückwärtssuche eingerückte
        # Zeilen nie und der Report meldet "gesperrt, aber keine Zeile" (Bug 28.07.).
        hits = [i for i, line in enumerate(raw_lines) if line.strip() == content]
        # Mehr Vorkommen als Verluste: der Parser hat einige behalten. Welche genau,
        # weiß der Round-Trip nicht — deshalb alle Fundstellen zeigen, aber ehrlich
        # sagen, wie viele davon tatsächlich fallen.
        for i in hits:
            found.append({
                "line": i + 1,
                "text": content.strip(),
                "item": _owner(raw_lines, i),
                "hint": _hint(content),
                "ambiguous": len(hits) > count,
            })
    found.sort(key=lambda d: d["line"])

    dup_ids, dup_themes = _duplicates(text, board, m)

    return {
        # locked haengt bewusst NUR am Round-Trip: Doppel sind ein Datenfehler,
        # aber kein Grund, jeden Schreibpfad mit 409 dichtzumachen.
        "locked": bool(missing),
        "lost": sum(missing.values()),
        "guard_lost": m.lost_total(text, board),
        "lines": found,
        "items": sum(1 for _ in m._all_items(board)),
        "thread": sum(len(it["thread"]) for _s, _n, _c, it in m._all_items(board)),
        "dup_ids": dup_ids,
        "dup_themes": dup_themes,
    }


def _dup_report(result: dict) -> list[str]:
    """Struktur-Doppel als eigener Block — sperrt nicht, muss aber gesehen werden."""
    out: list[str] = []
    for d in result.get("dup_themes", []):
        out += [f"  Themen-Überschrift \"## {d['name']}\" steht {d['count']}× "
                f"(Zeile {', '.join(str(x) for x in d['lines'])})"]
    for d in result.get("dup_ids", []):
        loc = ", ".join(str(x) for x in d["lines"]) or "?"
        out += [f"  @gc-id {d['id']} an {d['count']} Items (Zeile {loc})"]
        for t in d["titles"]:
            out += [f"      {t}"]
    if not out:
        return []
    return ([f"⚠ STRUKTUR — {len(result['dup_themes'])} doppelte Überschrift(en), "
             f"{len(result['dup_ids'])} doppelte @gc-id(s)", ""] + out +
            ["", "Doppel sperren das Board nicht, aber Schreibpfade treffen dann die",
             "ERSTE Kopie — Arbeitsstände laufen auseinander. Eine Kopie je Paar",
             "entfernen, dabei die neuere behalten.", ""])


def _report(result: dict, path: Path) -> str:
    dup = _dup_report(result)
    if not result["locked"]:
        head = (f"OK — {path}: {result['items']} Items, {result['thread']} Faden-Turns, "
                f"keine verlorenen Zeilen. Schreibpfade sind frei.")
        return head if not dup else "\n".join([head, ""] + dup)

    out = [f"GESPERRT — {result['lost']} Zeile(n) würde ein Save vernichten ({path})", ""]
    for e in result["lines"]:
        mark = "  (mehrdeutig: Zeile kommt mehrfach vor)" if e["ambiguous"] else ""
        out += [f"  Zeile {e['line']}:  {e['text'][:120]}{mark}",
                f"      am Item: {e['item']}",
                f"      {e['hint']}", ""]
    out += ["Solange das so ist, blockt der Server JEDEN Schreibpfad mit HTTP 409:",
            "neues Item, Agent-Run, Faden-Antwort, Chat, Capture. Zeile reparieren -> frei.", ""]
    if result["lost"] != result["guard_lost"]:
        out += [f"Hinweis: Server-Guard zählt {result['guard_lost']}, Round-Trip findet "
                f"{result['lost']}. Abweichung heißt, serialize_board normalisiert etwas —",
                "die Zeilenliste oben kann dann Fehlalarme enthalten. Siehe Modul-Docstring.", ""]
    out += dup
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Zeigt, welche board.md-Zeilen ein Save vernichten würde.")
    ap.add_argument("board", nargs="?", default=str(DEFAULT_BOARD), help="Pfad zur board.md")
    ap.add_argument("--json", action="store_true", help="Maschinenlesbar (für Skripte/Agenten)")
    args = ap.parse_args()

    path = Path(args.board)
    if not path.exists():
        print(f"Board nicht gefunden: {path}", file=sys.stderr)
        return 2

    result = lint(path.read_text())
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else _report(result, path))
    return 1 if result["locked"] or result["dup_ids"] or result["dup_themes"] else 0


if __name__ == "__main__":
    sys.exit(main())
