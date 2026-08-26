#!/usr/bin/env python3
"""radar_watch — der Radar meldet sich nur, wenn sich draussen wirklich etwas bewegt hat.

WAS: Laesst `dev_radar.py` den Live-Zustand aller MR/PR-Referenzen holen, vergleicht jeden
Befund mit dem letzten Sweep und behaelt nur die NEUEN. Fuer jeden neuen Befund liest ein
kleiner headless Agent die echte Konversation und schreibt ein URTEIL als `@gc-sys:`-Turn
in den Faden des Items.

WARUM: `dev_radar.py` kennt nur Regeln ("offen + 57 Tage still -> nachfassen"). Das ist
manchmal falsch: bei einer MR stand im letzten Kommentar "can be tackled once the
following is done: <link>" — das Ding war BLOCKIERT, nicht haengend, Nachfassen waere
der falsche Zug gewesen. Und eine Anzeige, die bei jedem Druck dieselben 13 Befunde
zeigt, erzieht zum Wegschauen. Die Anforderung dahinter: "Ich will nur wissen, wenn
nichts passiert ist, muss auch nichts angezeigt werden. Und wenn jetzt ein Review war,
ein Kommentar war, irgendwas anderes war, dann will ich es erzaehlt bekommen."

WIE:
    python3 -m superboard.radar_watch --dry-run   # zeigt, was gemeldet WUERDE
    python3 -m superboard.radar_watch             # meldet (Agent + Faden-Turn)
    python3 -m superboard.radar_watch --no-agent  # meldet ohne Agent (Skripttext)

DREI EIGENSCHAFTEN, die hier bewusst so gebaut sind:

1. ERSTER LAUF LERNT NUR. Fehlt die Zustandsdatei, wird kein einziger Turn geschrieben —
   sie wird nur angelegt. Damit ist die "13 Befunde auf einmal"-Flut nach einem
   Zustandsverlust strukturell unmoeglich und nicht bloss gedeckelt.
2. FINGERPRINT AUF DEN AUSLOESER, nicht auf die Uhr. Ein Befund ist derselbe, solange der
   ausloesende Kommentar / Approver / Zustand derselbe ist (`stamp` in dev_radar._f).
   "Seit 57 Tagen still" zaehlt taeglich hoch — als Schluessel taugt nur der Zeitpunkt der
   letzten echten Bewegung, sonst meldet dieselbe stille MR jeden Morgen neu.
3. FEHLSCHLAG SCHREIBT NICHTS. Faellt der Agent aus, gibt es keinen Turn mit
   deterministischem Ersatztext, der wie ein Urteil aussieht — der Befund bleibt einfach
   ungemeldet und kommt beim naechsten Sweep wieder. Lieber nichts als falsche Autoritaet.

GRENZE NACH DRAUSSEN: Der Agent darf LESEN (`glab mr view`, `gh pr view/diff`) und in das
Board des Owners schreiben. Er darf NIE in fremden Repos kommentieren, mergen oder
pushen — was rausgehen muesste, kommt als Entwurf in den Turn.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dev_radar  # noqa: E402
from server import claude_binary  # noqa: E402  (dev_radar zieht server ohnehin schon)

STATE_FILE = Path(__file__).resolve().parent / "journal" / "radar-state.json"
BOARD_URL = "http://127.0.0.1:47822"
MAX_DELTAS = 8          # Deckel pro Sweep — ein Ausreisser darf den Faden nicht fluten
JUDGE_MODEL = "sonnet"  # billiger Leser; das Urteil ist kurz, die Arbeit ist Lesen
JUDGE_TIMEOUT = 420     # Notbremse pro Urteil (s)
TURN_MAX = 460          # unter sidecar.INLINE_MAX (500): Radar-Turns bleiben Einzeiler

# Nur Refs, deren Repo EINDEUTIG feststeht, bekommen einen Agenten. Eine per Stichwort
# geratene Ref plus Agentensprache ergibt ueberzeugenden Unsinn — genau der Fehlermodus,
# vor dem der dev_radar-Docstring warnt ("plausible, aber FALSCHE Daten").
SAFE_RESOLVERS = {"pin", "url", "@ref", "qualified"}

# Nur diese Befunde sind eine Meldung wert. `watch`/`info` wandern in den Zustand
# (damit sie spaeter nicht als "neu" auffallen), aber nicht in den Faden.
REPORT_SEVERITY = {"action"}


# ---------------------------------------------------------------- Zustand

def load_state(path: Path = STATE_FILE) -> dict | None:
    """None = es gab noch nie einen Lauf (dann wird NICHTS gemeldet, nur gelernt)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and "reported" in data else None
    except (json.JSONDecodeError, OSError):
        return None  # kaputter Zustand = wie kein Zustand: lernen, nicht fluten


def save_state(reported: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "reported": reported}, ensure_ascii=False, indent=1))
    tmp.replace(path)


def fingerprint(gc_id: str, f: dict) -> str:
    """Identitaet eines Befunds: Item + Ref + Art + kausaler Ausloeser. Bewusst OHNE
    Befundtext — der traegt Tageszahlen ("seit 57d"), die sich jede Nacht aendern."""
    raw = f"{gc_id}|{f.get('url', '')}|{f.get('type', '')}|{f.get('stamp', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- Deltas

def collect(res: dict) -> dict:
    """Alle Befunde des Sweeps als {fingerprint: kontext}. Kontext traegt alles, was
    Urteil und Turn brauchen — der Aufrufer muss nicht mehr ins Radar-JSON zurueck."""
    out: dict[str, dict] = {}
    for it in res.get("items", []):
        by_url = {r.get("url"): r for r in it.get("refs", []) if r.get("url")}
        for f in it.get("findings", []):
            ref = by_url.get(f.get("url")) or {}
            out[fingerprint(it.get("gc_id", ""), f)] = {
                "gc_id": it.get("gc_id", ""), "title": it.get("title", ""),
                "type": f.get("type", ""), "severity": f.get("severity", ""),
                "text": f.get("text", ""), "url": f.get("url", ""),
                "label": ref.get("ref", ""), "host": ref.get("host", ""),
                "safe": ref.get("resolved_by", "") in SAFE_RESOLVERS,
            }
    return out


def live_urls(res: dict) -> set[str]:
    """URLs der Refs, deren Live-Zustand dieser Sweep WIRKLICH gesehen hat.

    Nur fuer die darf ein alter Fingerprint verfallen. Sonst wuerde ein einziger
    Netzaussetzer (glab/gh nicht erreichbar -> keine Befunde) den ganzen Zustand
    leerraeumen und am naechsten Tag alles erneut melden."""
    return {r["url"] for it in res.get("items", []) for r in it.get("refs", []) if r.get("url")}


def prune(old: dict, current: dict, seen: set[str]) -> dict:
    keep = {}
    for fp, entry in old.items():
        if fp in current:
            keep[fp] = entry            # gilt weiter
        elif (entry.get("u") or "") and entry["u"] not in seen:
            keep[fp] = entry            # Ref diesmal nicht erreichbar -> nichts vergessen
        # sonst: Befund ist nachweislich weg -> darf spaeter erneut melden (CI rot->gruen->rot)
    return keep


def deltas(current: dict, state: dict) -> list[tuple[str, dict]]:
    known = set(state.get("reported") or {})
    new = [(fp, d) for fp, d in current.items()
           if fp not in known and d["severity"] in REPORT_SEVERITY and d["gc_id"]]
    prio = {"ready_for_you": 0, "merged": 1, "comment_unanswered": 2, "ci_red": 3}
    new.sort(key=lambda x: prio.get(x[1]["type"], 9))
    return new


# ---------------------------------------------------------------- Urteil

JUDGE_PROMPT = """Du pruefst EINEN Radar-Befund fuer das To-do-Board des Owners. Ein \
Skript hat nach festen Regeln gemeldet — du sagst, was WIRKLICH los ist.

Board-Item: "{title}"
Befund des Skripts: {text}
MR/PR: {url}

Auftrag: Lies die echte Konversation und, wenn noetig, den Diff. Nur LESEN:
`glab mr view <nr> --repo <projekt> --comments` bzw. `gh pr view <nr> --repo <repo> --comments`,
`gh pr diff`. Du darfst in fremden Repos NICHTS schreiben: nicht kommentieren, nicht mergen,
nicht pushen. Muss etwas rausgehen, schreib den Text als Entwurf.

Achte besonders darauf, ob der Ball wirklich beim Owner liegt: "seit X Tagen still" heisst
oft "blockiert durch etwas anderes" oder "laengst erledigt", nicht "nachfassen".

Antworte mit GENAU EINER Zeile JSON, ohne Markdown-Zaun:
{{"verdict": "was wirklich los ist, max 12 Woerter", "action": "was der Owner als naechstes \
tun sollte, max 18 Woerter", "quote": "der ausloesende Satz von draussen, max 14 Woerter, \
leer wenn keiner", "draft": "fertiger Text falls etwas rausgehen muesste, sonst leer"}}"""


def judge(d: dict, claude_cmd: str) -> dict | None:
    """Ein Urteil oder None. None heisst: kein Turn — der Befund kommt naechsten Sweep wieder."""
    import gc_runner
    prompt = JUDGE_PROMPT.format(title=d["title"][:80], text=d["text"][:400], url=d["url"])
    out = gc_runner.spawn_claude(prompt, "", claude_cmd, JUDGE_TIMEOUT, model=JUDGE_MODEL)
    if not out.get("ok"):
        print(f"  ! Urteil fehlgeschlagen ({d['label']}): "
              f"{(out.get('raw_error') or out.get('reply') or '')[:120]}", file=sys.stderr)
        return None
    reply = (out.get("reply") or "").strip()
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        print(f"  ! Urteil ohne JSON ({d['label']}): {reply[:120]}", file=sys.stderr)
        return None
    try:
        data = json.loads(reply[start:end + 1])
    except json.JSONDecodeError:
        print(f"  ! Urteil unlesbar ({d['label']}): {reply[start:start + 120]}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or not (data.get("verdict") or data.get("action")):
        return None
    return data


def short_label(label: str) -> str:
    """`my-org/my-service-monorepo/service-name!343` -> `service-name!343`.
    Der Faden-Turn ist ein Einzeiler; der volle Projektpfad frisst die halbe Zeile,
    und der Link darunter traegt die eindeutige Adresse ohnehin."""
    if not label:
        return ""
    sep = "!" if "!" in label else "#"
    repo, _, num = label.rpartition(sep)
    tail = repo.rsplit("/", 1)[-1]
    return f"{tail}{sep}{num}" if num else label


def turn_text(d: dict, verdict: dict | None) -> str:
    """Ein Einzeiler, der ohne Klick verstaendlich ist — und seine Quelle mitbringt.
    Die Quelle ist Pflicht: ein Agentensatz liest sich sicherer als er ist, und nur mit
    Link + Zitat ist ein Fehlurteil in fuenf Sekunden widerlegbar."""
    head = f"📡 Radar · {short_label(d['label']) or d['type']}"
    if verdict is None:
        # Der Skripttext beginnt mit demselben Label wie der Kopf — nicht doppeln.
        body = d["text"]
        if d["label"] and body.startswith(d["label"]):
            body = body[len(d["label"]):].lstrip(": ")
    else:
        parts = [str(verdict.get("verdict") or "").strip()]
        if verdict.get("action"):
            parts.append("→ " + str(verdict["action"]).strip())
        if verdict.get("quote"):
            parts.append(f"Ausloeser: „{str(verdict['quote']).strip()}“")
        if verdict.get("draft"):
            parts.append(f"Entwurf: „{str(verdict['draft']).strip()[:160]}“")
        body = " · ".join(p for p in parts if p)
    line = f"{head} · {body}"
    if d["url"]:
        line = f"{line} · {d['url']}"
    line = " ".join(line.split())
    return line if len(line) <= TURN_MAX else line[:TURN_MAX - 1] + "…"


# ---------------------------------------------------------------- Board

def append_turn(gc_id: str, text: str, base_url: str = BOARD_URL) -> bool:
    """Schreibt ueber den Server (Single-Writer), nie direkt in board.md."""
    payload = {"kind": "sys", "text": text, "addr": {"id": gc_id}}
    req = urllib.request.Request(f"{base_url}/api/gc-append",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.load(r)
        return True
    except Exception as e:  # noqa: BLE001 — ein toter Server darf den Sweep nicht sprengen
        print(f"  ! Board-Append fehlgeschlagen ({gc_id}): {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------- Sweep

def sweep(board_path: Path, theme: str, *, dry_run: bool = False, use_agent: bool = True,
          limit: int = MAX_DELTAS, base_url: str = BOARD_URL, seed_report: int = 0,
          state_file: Path = STATE_FILE) -> dict:
    """Ein Durchlauf: Zustand holen, Deltas bilden, urteilen, in den Faden schreiben."""
    res = dev_radar.run(board_path, theme)
    current = collect(res)
    state = load_state(state_file)
    first = state is None

    new = deltas(current, state or {"reported": {}})
    if first:
        # Erstlauf (oder Zustandsverlust): NICHT alles melden. Ohne diese Regel waere ein
        # geloeschtes Journal ein Flutungsereignis. `--seed-report N` laesst bewusst die N
        # wichtigsten durch — zum Anschauen, wenn der Melder neu ist.
        allowed = len(new) if dry_run else seed_report
        print(f"Erstlauf: {len(current)} Befunde, {len(new)} davon meldenswert — "
              f"gemeldet werden {min(allowed, len(new))}, der Rest wird als "
              f"Ausgangszustand gelernt.")
        new = new[:allowed]
    else:
        print(f"Sweep: {len(current)} Befunde, {len(new)} neu"
              + (f" (Deckel {limit})" if len(new) > limit else ""))

    reported_now, unreported, capped = [], [], new[:limit]
    for fp, d in capped:
        agentable = use_agent and d["safe"]
        verdict = judge(d, claude_binary()) if agentable else None
        if agentable and verdict is None:
            unreported.append(fp)
            continue  # Fehlschlag: lieber kein Turn als ein falsches Urteil
        text = turn_text(d, verdict)
        print(f"  {'[dry] ' if dry_run else ''}{d['title'][:38]} :: {text[:150]}")
        if dry_run:
            continue
        if append_turn(d["gc_id"], text, base_url):
            reported_now.append(fp)
        else:
            unreported.append(fp)

    if not dry_run:
        # Alte Fingerprints, die nachweislich weg sind, fallen raus — damit ein Befund, der
        # verschwindet und spaeter wiederkommt (CI rot -> gruen -> rot), erneut meldet.
        # "Nachweislich" ist das Wort: siehe prune()/live_urls().
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        keep = prune((state or {}).get("reported") or {}, current, live_urls(res))
        for fp in unreported:
            # Ein VERSUCHTER, aber gescheiterter Befund darf nicht als "gesehen" in den
            # Zustand — sonst schluckt ein Server-Fehler oder ein Agent-Timeout die Meldung
            # fuer immer (real passiert: mehrere Urteile standen im Log, der Append schlug
            # fehl, und der Zustand lernte sie trotzdem als `quiet`). Eigenschaft 3 im Kopf
            # dieser Datei verspricht das Gegenteil.
            keep.pop(fp, None)
        for fp in reported_now:
            keep[fp] = {"first": stamp, "item": current[fp]["gc_id"],
                        "type": current[fp]["type"], "u": current[fp]["url"]}
        for fp in current:  # gesehen, aber (noch) nicht gemeldet: watch/info, Deckel, Erstlauf
            if fp in unreported:
                continue
            keep.setdefault(fp, {"first": stamp, "quiet": True, "u": current[fp]["url"]})
        save_state(keep, state_file)

    return {"first": first, "reported": reported_now, "unreported": unreported,
            "deltas": len(new), "seen": len(current)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Radar-Deltas als Faden-Turns melden")
    ap.add_argument("--file", type=Path, default=dev_radar.DEFAULT_BOARD)
    ap.add_argument("--theme", default=dev_radar.DEFAULT_THEME)
    ap.add_argument("--dry-run", action="store_true", help="nur zeigen, nichts schreiben")
    ap.add_argument("--no-agent", action="store_true", help="ohne Urteil, nur Skripttext")
    ap.add_argument("--limit", type=int, default=MAX_DELTAS)
    ap.add_argument("--url", default=BOARD_URL)
    ap.add_argument("--seed-report", type=int, default=0, metavar="N",
                    help="nur beim ERSTEN Lauf: N wichtigste Befunde trotzdem melden")
    args = ap.parse_args()
    sweep(args.file, args.theme, dry_run=args.dry_run, use_agent=not args.no_agent,
          limit=args.limit, base_url=args.url, seed_report=args.seed_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
