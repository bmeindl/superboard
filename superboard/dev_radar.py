#!/usr/bin/env python3
"""dev_radar — Live-Status der offenen Dev-Themen auf dem To-do-Board.

WAS: Liest eine Dev-Themen-Zeile aus `inbox/board.md`, zieht zu jedem Item
die referenzierten GitLab-MRs / GitHub-PRs live (via `glab` / `gh`) und leitet daraus
Befunde ab. Die Leitfrage pro Item ist: **"hat sich draussen was bewegt — und wartet
es jetzt auf den OWNER?"**  (approved → mergen · Kommentar offen → antworten ·
CI rot → fixen · gemerged → Item zu).

WARUM: Das Board sagt, was der Owner tun WOLLTE. Die Repos sagen, was jetzt WIRKLICH
ansteht. Board-Text veraltet still (z.B. "Kommentar unbeantwortet", obwohl laengst
geantwortet wurde) — dieses Skript ersetzt Erinnerung durch Live-Wahrheit.

WIE:
    python3 superboard/dev_radar.py                  # kompakte Textzusammenfassung
    python3 superboard/dev_radar.py --json           # maschinenlesbar (fuer den Board-Server)
    python3 superboard/dev_radar.py --theme "Dev" --file /pfad/board.md

READ-ONLY gegenueber dem Board: dieses Skript schreibt NIE nach board.md (der
todo-board-Server ist Single-Writer). Stdlib only, keine pip-Deps — wie server.py.

REF-AUFLOESUNG (der heikle Teil, bitte lesen bevor du an den Regeln drehst):
Eine nackte Referenz wie `!343` oder `#6` ist NICHT eindeutig — dieselbe Nummer
existiert typischerweise in mehreren Repos mit voellig anderem Inhalt (verifiziert
2026-07-14 in der Ursprungs-Instanz). Eine falsch aufgeloeste Ref liefert plausible,
aber FALSCHE Daten — der schlimmste Fehlermodus. Darum die Prioritaetskette:

    1. volle URL im Text                      -> sicher
    2. `@ref:`-Zeile im Item-Body             -> manuell gepinnt (siehe unten)
    3. REF_PINS (Tabelle unten)               -> verifiziert gepinnt
    4. Hint-Keyword in DERSELBEN Zeile        -> gut
    5. Hint-Keyword irgendwo im Item          -> unsicher (wird als solches markiert)
    6. sonst                                  -> KEIN Rateschluss, Finding `unknown`

Jede Ref traegt im JSON ein `resolved_by`-Feld — eine Fehlauflösung ist damit
sichtbar statt still. Zum Pinnen einer Ref direkt im Board reicht eine Body-Zeile:

    @ref: gh:my-org/my-service#6
    @ref: gl:my-group/my-service!343
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import KNOWN_COLUMNS, parse_board  # noqa: E402

import config as _cfg  # noqa: E402
import paths as _p  # noqa: E402

ROOT = _p.HERE
DEFAULT_BOARD = _p.BOARD
DEFAULT_THEME = "Dev"

# ---------------------------------------------------------------- Schwellen (drehbar)
REVIEW_STALE_DAYS = 7        # offen + kein Approval/Review-Fortschritt seit N Tagen
COMMENT_UNANSWERED_DAYS = 3  # letzter Kommentar von jemand anderem, seit N Tagen offen
NO_MOVEMENT_DAYS = 14        # gar keine Aktivitaet seit N Tagen
CLI_TIMEOUT = 20             # Sekunden pro CLI-Call
MAX_WORKERS = 8              # parallele CLI-Calls

# Identitaeten des Nutzers - "wartet auf mich?" haengt daran, wer zuletzt geredet hat.
# Instanz-spezifisch, deshalb aus board.config.json (Default: leer = niemand ist "ich").
ME_GITLAB = set(_cfg.IDENTITIES.get("gitlab", []))
ME_GITHUB = set(_cfg.IDENTITIES.get("github", []))
# Bots reden viel und warten auf niemanden -> zaehlen nicht als "jemand hat dir geantwortet".
BOT_AUTHORS = {"coderabbitai", "coderabbitai[bot]", "github-actions", "github-actions[bot]",
               "gitlab-bot", "dependabot", "dependabot[bot]", "renovate", "renovate[bot]"}

# Instanz-Konfiguration: gepinnte Refs und Keyword->Repo-Heuristiken sind pro
# Installation verschieden. Defaults leer — nackte Refs (!123 / #6) bleiben dann
# unaufgeloest, volle URLs und `@ref:`-Zeilen funktionieren immer.
REF_PINS: dict[str, str] = {}

# Keyword -> Projekt, getrennt pro Host. Beispiel:
#   GITLAB_HINTS = [(r"frontend", "my-group/my-frontend-repo")]
GITLAB_HINTS: list[tuple[str, str]] = []
GITHUB_HINTS: list[tuple[str, str]] = []

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------- Ref-Extraktion

REF_ANNOTATION_RE = re.compile(r"@ref:\s*(gh|gl):([^\s#!]+)\s*[#!](\d+)", re.I)
GL_URL_RE = re.compile(r"https?://gitlab\.com/([\w\-./]+?)/-/merge_requests/(\d+)")
GH_URL_RE = re.compile(r"https?://github\.com/([\w\-.]+/[\w\-.]+)/pull/(\d+)")
# owner/repo#N oder repo#N
QUALIFIED_GH_RE = re.compile(r"\b([\w\-.]+/[\w\-.]+|[\w\-.]{4,})#(\d+)\b")
# Nackte GitLab-MR-Ref: !475. Die Board-Due-Syntax !(2026-07-13) matcht nicht (Klammer
# statt Ziffer). Lookbehind bewusst NUR \w — ein "/" davor muss erlaubt bleiben, sonst
# fallen Refs in Slash-Listen wie "!475/!479/!481" ab dem zweiten still unter den Tisch.
# URLs sind zu diesem Zeitpunkt schon rausgeschnitten, koennen hier also nicht reinfunken.
BARE_GL_RE = re.compile(r"(?<!\w)!(\d{1,5})\b")
# Nackte GitHub-PR-Ref: "PR #6" / "#6" (dito Slash-Listen).
BARE_GH_RE = re.compile(r"(?<!\w)#(\d{1,5})\b")
# Ticket-Referenzen (optional): Key-Muster des eigenen Trackers, z.B. r"\b(PROJ-\d{3,5})\b".
# Leer = Feature aus.
JIRA_PATTERN = ""
JIRA_RE = re.compile(JIRA_PATTERN) if JIRA_PATTERN else None


def _hint_lookup(text: str, hints: list[tuple[str, str]]) -> list[str]:
    """Alle Projekte, deren Keyword im Text vorkommt (Reihenfolge = Prioritaet)."""
    return [proj for pat, proj in hints if re.search(pat, text, re.I)]


def _resolve(host: str, num: str, line: str, item_text: str) -> tuple[str | None, str]:
    """Projekt fuer eine nackte Ref bestimmen. -> (projekt|None, resolved_by).

    Bewusst KEIN Fallback auf ein Default-Repo: lieber ein ehrliches `unknown` als
    eine plausible Luege (dieselbe MR-Nummer existiert in mehreren Projekten)."""
    key = f"{host}{'!' if host == 'gl' else '#'}{num}"
    if pinned := REF_PINS.get(key):
        return pinned, "pin"
    hints = GITLAB_HINTS if host == "gl" else GITHUB_HINTS
    if line_hits := _hint_lookup(line, hints):
        return line_hits[0], "hint:line"
    item_hits = _hint_lookup(item_text, hints)
    if len(set(item_hits)) == 1:  # nur wenn das Item EINDEUTIG auf ein Projekt zeigt
        return item_hits[0], "hint:item"
    return None, "ambiguous" if item_hits else "unresolved"


def extract_refs(title: str, body: list[str]) -> list[dict]:
    """Alle Referenzen aus Titel+Body. Keine Ref zu finden ist voellig ok -> [].

    `in_title` merkt sich, ob die Ref im Titel stand — daraus entscheidet sich spaeter,
    welchem Item die Befunde GEHOEREN (Items zitieren fremde MRs auch mal nur en
    passant im Fliesstext, z.B. „nach Abschluss der aktiven MRs (!475/!479/!481)")."""
    lines = [title] + list(body)
    item_text = "\n".join(lines)
    refs: list[dict] = []
    seen: dict[tuple[str, str, str], dict] = {}

    def add(host: str, project: str | None, num: str, how: str) -> None:
        k = (host, project or "?", num)
        cur = seen.get(k)
        if cur is not None:
            if line is title:      # dieselbe Ref spaeter nochmal, aber im Titel:
                cur["in_title"] = True   # Besitz haengt daran (assign_owners)
            return
        r = {"host": host, "project": project, "number": int(num),
             "resolved_by": how, "in_title": line is title,
             "ref": f"{project or '?'}{'!' if host == 'gl' else '#'}{num}"}
        seen[k] = r
        refs.append(r)

    # Vorlauf: was das Item SELBST eindeutig macht (@ref-Zeile, volle URL), gilt fuer
    # jede nackte Erwaehnung derselben Nummer im selben Item. Ohne das produzierte ein
    # gepinntes Item fuer sein eigenes „#27" im Titel weiter ein `unresolved`-Finding
    # neben dem echten Befund — Rauschen genau dort, wo jemand sauber gepinnt hat.
    explicit: dict[tuple[str, str], tuple[str, str]] = {}
    for line in lines:
        for m in REF_ANNOTATION_RE.finditer(line):
            explicit.setdefault(("gh" if m.group(1).lower() == "gh" else "gl", m.group(3)),
                                (m.group(2), "@ref"))
        for m in GL_URL_RE.finditer(line):
            explicit.setdefault(("gl", m.group(2)), (m.group(1), "url"))
        for m in GH_URL_RE.finditer(line):
            explicit.setdefault(("gh", m.group(2)), (m.group(1), "url"))

    for line in lines:
        # 1./2. explizit: @ref:-Annotation und volle URLs
        for m in REF_ANNOTATION_RE.finditer(line):
            add("gh" if m.group(1).lower() == "gh" else "gl", m.group(2), m.group(3), "@ref")
        for m in GL_URL_RE.finditer(line):
            add("gl", m.group(1), m.group(2), "url")
        for m in GH_URL_RE.finditer(line):
            add("gh", m.group(1), m.group(2), "url")

        stripped = GL_URL_RE.sub(" ", GH_URL_RE.sub(" ", REF_ANNOTATION_RE.sub(" ", line)))

        # 3. qualifiziert: repo#N
        for m in QUALIFIED_GH_RE.finditer(stripped):
            repo = m.group(1)
            if "/" not in repo:
                repo = f"{GH_ORG}/{repo}"
            add("gh", repo, m.group(2), "qualified")
        stripped = QUALIFIED_GH_RE.sub(" ", stripped)

        # 4.-6. nackte Refs -> Pins/Hints, sonst unknown
        for host, rx in (("gl", BARE_GL_RE), ("gh", BARE_GH_RE)):
            for m in rx.finditer(stripped):
                pin = explicit.get((host, m.group(1)))
                if pin:
                    add(host, pin[0], m.group(1), pin[1])
                    continue
                proj, how = _resolve(host, m.group(1), line, item_text)
                add(host, proj, m.group(1), how)

    for m in (JIRA_RE.finditer(item_text) if JIRA_RE else ()):  # nur gelistet — kein CLI fuer Jira-Live-Status
        key = m.group(1)
        if ("jira", key, "") not in seen:
            seen[("jira", key, "")] = {}
            refs.append({"host": "jira", "project": None, "number": None,
                         "resolved_by": "text", "in_title": key in title,
                         "ref": key, "checked": False})
    return refs


def assign_owners(items: list[dict]) -> None:
    """Jede Ref genau EINEM Item zuordnen (`owner`), damit ein MR-Befund nicht mehrfach
    auftaucht. Regel: Item, das die Ref im TITEL fuehrt, gewinnt; sonst das erste Item
    in Board-Reihenfolge. Alle anderen behalten die Ref als Kontext (`owner: False`),
    bekommen aber keine Befunde."""
    def key(r: dict) -> tuple:
        return (r["host"], r.get("project") or "?", r["ref"], r["number"])

    owner: dict[tuple, dict] = {}
    for it in items:  # Runde 1: Titel-Treffer duerfen zuerst zugreifen
        for r in it["refs"]:
            if r.get("in_title") and key(r) not in owner:
                owner[key(r)] = r
    for it in items:  # Runde 2: der Rest nach Board-Reihenfolge
        for r in it["refs"]:
            owner.setdefault(key(r), r)
    for it in items:
        for r in it["refs"]:
            r["owner"] = owner[key(r)] is r


# ---------------------------------------------------------------- CLI-Layer

class CliError(Exception):
    pass


def _run(cmd: list[str]) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        raise CliError(f"`{cmd[0]}` nicht installiert")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise CliError(f"`{cmd[0]}` Timeout nach {CLI_TIMEOUT}s") from None
    except OSError as e:
        raise CliError(f"`{cmd[0]}` nicht startbar: {e}") from None
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit {p.returncode}"
        raise CliError(msg[:200])
    return p.stdout


def _run_json(cmd: list[str]):
    out = _run(cmd)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise CliError(f"kein valides JSON von `{cmd[0]}`") from None


def _days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return (NOW - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 86400
    except ValueError:
        return None


def _enc(project: str) -> str:
    return urllib.parse.quote(project, safe="")


def fetch_gitlab(project: str, iid: int) -> dict:
    """MR-Zustand + Approvals + Notes. Wirft CliError -> wird oben zu `unknown`."""
    p = _enc(project)
    mr = _run_json(["glab", "api", f"projects/{p}/merge_requests/{iid}"])
    try:
        appr = _run_json(["glab", "api", f"projects/{p}/merge_requests/{iid}/approvals"])
    except CliError:
        appr = {}
    try:
        notes = _run_json(["glab", "api",
                           f"projects/{p}/merge_requests/{iid}/notes?per_page=100&sort=desc"])
    except CliError:
        notes = []

    pipeline = (mr.get("head_pipeline") or mr.get("pipeline") or {}).get("status")
    human = [n for n in notes if not n.get("system")
             and (n.get("author") or {}).get("username") not in BOT_AUTHORS]
    human.sort(key=lambda n: n.get("created_at") or "")
    last = human[-1] if human else None
    approvers = [(a.get("user") or {}).get("username") for a in (appr.get("approved_by") or [])]
    return {
        "kind": "mr", "state": mr.get("state"), "title": mr.get("title", ""),
        "url": mr.get("web_url", ""), "draft": bool(mr.get("draft")),
        "created_at": mr.get("created_at"), "updated_at": mr.get("updated_at"),
        "ci": {"success": "green", "failed": "red", "running": "running",
               "canceled": "canceled"}.get(pipeline, pipeline),
        "approved": bool(appr.get("approved")) or bool(approvers),
        "approvers": approvers,
        "reviewers": [(r or {}).get("username") for r in (mr.get("reviewers") or [])],
        "last_comment": ({"author": (last.get("author") or {}).get("username"),
                          "at": last.get("created_at"),
                          "body": (last.get("body") or "")[:160]} if last else None),
        "changes_requested_by": [], "last_review_at": None,
        "me": ME_GITLAB,
    }


def fetch_github(repo: str, num: int) -> dict:
    """PR-Zustand + Reviews + Issue- UND Inline-Review-Kommentare."""
    fields = ("number,title,state,url,createdAt,updatedAt,mergedAt,reviewDecision,"
              "reviews,comments,statusCheckRollup,isDraft")
    pr = _run_json(["gh", "pr", "view", str(num), "-R", repo, "--json", fields])
    try:  # Inline-Review-Kommentare (genau die Sorte, die Reviews blockiert)
        inline = _run_json(["gh", "api", f"repos/{repo}/pulls/{num}/comments?per_page=100"])
    except CliError:
        inline = []

    events: list[dict] = []
    for c in pr.get("comments") or []:
        events.append({"author": (c.get("author") or {}).get("login"),
                       "at": c.get("createdAt"), "body": c.get("body") or ""})
    for c in inline if isinstance(inline, list) else []:
        events.append({"author": (c.get("user") or {}).get("login"),
                       "at": c.get("created_at"), "body": c.get("body") or ""})
    reviews = pr.get("reviews") or []
    for r in reviews:  # ein Review mit Body ist auch ein Kommentar
        if (r.get("body") or "").strip():
            events.append({"author": (r.get("author") or {}).get("login"),
                           "at": r.get("submittedAt"), "body": r.get("body") or ""})
    human = [e for e in events if e["author"] not in BOT_AUTHORS and e["at"]]
    human.sort(key=lambda e: e["at"])
    last = human[-1] if human else None

    rollup = pr.get("statusCheckRollup") or []
    concl = [(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    ci = None
    if concl:
        if any(c in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for c in concl):
            ci = "red"
        elif any(c in ("PENDING", "IN_PROGRESS", "QUEUED", "") for c in concl):
            ci = "running"
        else:
            ci = "green"

    real = [r for r in reviews if r.get("state") in
            ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")]
    real.sort(key=lambda r: r.get("submittedAt") or "")
    # Nur der JEWEILS LETZTE Review je Person zaehlt (ein spaeteres APPROVED hebt
    # ein frueheres CHANGES_REQUESTED auf).
    latest: dict[str, dict] = {}
    for r in real:
        who = (r.get("author") or {}).get("login")
        if who and who not in ME_GITHUB:
            latest[who] = r
    cr = [(w, r.get("submittedAt")) for w, r in latest.items()
          if r.get("state") == "CHANGES_REQUESTED"]
    external = [r for r in real if (r.get("author") or {}).get("login") not in ME_GITHUB]

    state = (pr.get("state") or "").lower()
    return {
        "kind": "pr", "state": "merged" if pr.get("mergedAt") else state,
        "title": pr.get("title", ""), "url": pr.get("url", ""),
        "draft": bool(pr.get("isDraft")),
        "created_at": pr.get("createdAt"), "updated_at": pr.get("updatedAt"),
        "ci": ci,
        "approved": (pr.get("reviewDecision") == "APPROVED"),
        "approvers": [w for w, r in latest.items() if r.get("state") == "APPROVED"],
        "reviewers": sorted(latest),
        "last_comment": ({"author": last["author"], "at": last["at"],
                          "body": last["body"][:160]} if last else None),
        "changes_requested_by": cr,
        "last_review_at": external[-1].get("submittedAt") if external else None,
        "me": ME_GITHUB,
    }


# ---------------------------------------------------------------- Befunde

def _f(t: str, sev: str, text: str, url: str = "", stamp: str = "") -> dict:
    """Ein Befund. `stamp` = der KAUSALE Ausloeser (Kommentar-Zeitstempel, Approver,
    Zustand) — nicht die Uhrzeit des Laufs. Nur daraus laesst sich spaeter entscheiden,
    ob ein Befund NEU ist oder derselbe wie gestern (radar_watch.py).
    Ohne stamp faellt der Fingerprint auf den Befundtyp zurueck: meldet einmal, dann Ruhe."""
    return {"type": t, "severity": sev, "text": text, "url": url, "stamp": stamp}


def _lc_hint(lc: dict | None, limit: int = 120) -> str:
    """Letzten Kommentar an einen review_stale-Befund haengen.

    Grund: „Review haengt seit 12d" sagt nicht, WARUM. Steht im letzten Kommentar
    „can be tackled once the following is done: …", ist der Ball gar nicht bei
    dir — das sieht man nur, wenn der Text mitkommt."""
    if not lc or not lc.get("author"):
        return ""
    snip = " ".join((lc.get("body") or "").split())[:limit]
    if not snip:
        return ""
    return f" | letzter Kommentar ({lc['author']}): „{snip}…“"


def _last_activity(st: dict, lc: dict | None) -> str:
    """Juengster echter Zeitstempel am Ref — der Anker fuer Idle-Befunde.

    Idle ist ein UHR-Ereignis („seit 57d nichts"), kein Repo-Ereignis: als Fingerprint
    taugt nur der Zeitpunkt der letzten echten Bewegung. Sonst meldete derselbe stille
    MR jeden Tag aufs Neue, weil die Tageszahl hochzaehlt."""
    cands = [st.get("updated_at"), (lc or {}).get("at"), st.get("last_review_at")]
    return max((str(c) for c in cands if c), default="")


def findings_for(ref: dict, st: dict) -> list[dict]:
    """Der Kern: aus dem Live-Zustand ableiten, ob das Ding auf den OWNER wartet."""
    out: list[dict] = []
    url = st.get("url", "")
    label = ref["ref"]
    me = st["me"]
    state = st["state"]

    if state == "merged":
        d = _days(st.get("updated_at"))
        out.append(_f("merged", "action",
                      f"{label} ist GEMERGED{f' (vor {d:.0f}d)' if d is not None else ''} — "
                      f"Board-Item noch offen: schliessen oder Folgeschritt.", url,
                      stamp=str(st.get("updated_at") or "merged")))
        return out
    if state in ("closed", "locked"):
        out.append(_f("closed", "info", f"{label} ist geschlossen (nicht gemerged).", url,
                      stamp=state))
        return out

    # --- ab hier: offen ---
    if st.get("draft"):
        out.append(_f("no_movement", "info", f"{label} ist noch Draft.", url, stamp="draft"))

    if st.get("ci") == "red":
        out.append(_f("ci_red", "action", f"{label}: Pipeline ROT — blockiert den Merge.", url,
                      stamp="red"))

    if st["approved"] and not st.get("draft"):
        who = ", ".join(a for a in st["approvers"] if a) or "Reviewer"
        ci_warn = " (aber CI rot!)" if st.get("ci") == "red" else ""
        out.append(_f("ready_for_you", "action",
                      f"{label} ist APPROVED von {who}{ci_warn} → wartet auf DICH: mergen.", url,
                      stamp=who))
        return out  # approved schlaegt alles andere — der Ball liegt beim Owner

    # Kommentar von jemand anderem, den der Owner nicht beantwortet hat?
    lc = st.get("last_comment")
    if lc and lc["author"] and lc["author"] not in me:
        age = _days(lc["at"])
        if age is not None and age >= COMMENT_UNANSWERED_DAYS:
            snip = " ".join((lc["body"] or "").split())[:70]
            out.append(_f("comment_unanswered", "action",
                          f"{label}: letzter Kommentar von {lc['author']} vor {age:.0f}d, "
                          f"von dir unbeantwortet — „{snip}…“", url,
                          stamp=str(lc["at"] or "")))

    # CHANGES_REQUESTED (GitHub): steht es noch, obwohl der Owner laengst geantwortet hat?
    for who, at in st.get("changes_requested_by") or []:
        age = _days(at)
        answered = bool(lc and lc["author"] in me and lc["at"] and at and lc["at"] > at)
        if answered and age is not None and age >= REVIEW_STALE_DAYS:
            out.append(_f("review_stale", "action",
                          f"{label}: CHANGES_REQUESTED von {who} seit {age:.0f}d — du hast am "
                          f"{(lc['at'] or '')[:10]} geantwortet, Re-Review steht aus → nachfassen."
                          + _lc_hint(lc), url, stamp=f"{who}|{at}|answered"))
        elif not answered and age is not None:
            out.append(_f("review_stale", "watch",
                          f"{label}: {who} fordert Aenderungen (seit {age:.0f}d) — Ball bei dir."
                          + _lc_hint(lc), url, stamp=f"{who}|{at}|open"))

    # Review haengt: offen, kein Approval, seit >N Tagen nichts passiert
    last_act = max((x for x in [_days(st.get("updated_at")),
                                _days((lc or {}).get("at")),
                                _days(st.get("last_review_at"))] if x is not None),
                   default=None)
    idle = min((x for x in [_days(st.get("updated_at")),
                            _days((lc or {}).get("at")),
                            _days(st.get("last_review_at"))] if x is not None), default=None)
    has_cr = bool(st.get("changes_requested_by"))
    if idle is not None and idle >= REVIEW_STALE_DAYS and not has_cr:
        rev = ", ".join(r for r in st.get("reviewers") or [] if r)
        who = f" bei {rev}" if rev else " (kein Reviewer gesetzt!)"
        out.append(_f("review_stale", "action" if rev else "watch",
                      f"{label}: offen, kein Approval, seit {idle:.0f}d ohne Bewegung — "
                      f"Review haengt{who} → nachfassen." + _lc_hint(lc), url,
                      stamp=_last_activity(st, lc)))
    elif idle is not None and idle >= NO_MOVEMENT_DAYS:
        out.append(_f("no_movement", "watch",
                      f"{label}: seit {idle:.0f}d keine Bewegung.", url,
                      stamp=_last_activity(st, lc)))
    _ = last_act
    return out


def analyse_ref(ref: dict) -> list[dict]:
    if ref["host"] == "jira":
        return []  # kein CLI fuer Jira -> nur als Ref gelistet, kein Live-Status
    if not ref.get("project"):
        return [_f("unknown", "watch",
                   f"Referenz {'!' if ref['host'] == 'gl' else '#'}{ref['number']} nicht "
                   f"aufloesbar ({ref['resolved_by']}) — Repo unklar. Fix: `@ref:`-Zeile ins "
                   f"Item (siehe Docstring) oder REF_PINS ergaenzen.")]
    try:
        st = (fetch_gitlab(ref["project"], ref["number"]) if ref["host"] == "gl"
              else fetch_github(ref["project"], ref["number"]))
    except CliError as e:
        return [_f("unknown", "watch", f"{ref['ref']}: Live-Status nicht abrufbar — {e}")]
    except Exception as e:  # noqa: BLE001 — das Skript crasht NIE
        return [_f("unknown", "watch", f"{ref['ref']}: unerwarteter Fehler — {type(e).__name__}: {e}")]
    ref["state"] = st["state"]
    ref["title"] = st["title"]
    ref["url"] = st["url"]
    return findings_for(ref, st)


# ---------------------------------------------------------------- Board -> Items

def _radar_columns(theme: dict) -> list[str]:
    """ALLE Spalten des Themas, nicht nur die drei Default-Spalten.

    Genau die Items, die auf jemand anderen warten, liegen in `Wartet auf andere` —
    und deren Frage ("hat er endlich approved?") IST die Leitfrage dieses Skripts.
    Bis 2026-08-24 lief die Schleife ueber `server.COLUMNS`, den Rueckwaerts-Alias
    auf die drei Default-Spalten: die Wartet-Spalte war damit strukturell unsichtbar
    (ein echtes Approval fiel genau da durch). Unbekannte Spalten kommen hinten dran,
    damit ein neues Board nichts stillschweigend verliert."""
    known = [c for c in KNOWN_COLUMNS if c in theme["cols"]]
    return known + [c for c in theme["cols"] if c not in KNOWN_COLUMNS]


def collect_items(board_path: Path, theme_name: str) -> list[dict]:
    board = parse_board(board_path.read_text())
    theme = next((t for t in board["themes"] if t["name"] == theme_name), None)
    if theme is None:
        return []
    items = []
    for col in _radar_columns(theme):
        for it in theme["cols"].get(col, []):
            if it.get("done"):
                continue
            items.append({"gc_id": it.get("id", ""), "title": it["title"], "col": col,
                          "refs": extract_refs(it["title"], it.get("body", [])),
                          "findings": []})
    return items


def run(board_path: Path, theme: str) -> dict:
    items = collect_items(board_path, theme)
    assign_owners(items)
    jobs: list[tuple[dict, dict]] = [(it, r) for it in items for r in it["refs"]
                                     if r["host"] != "jira" and r["owner"]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(analyse_ref, r): (it, r) for it, r in jobs}
        for fut in concurrent.futures.as_completed(futs):
            it, r = futs[fut]
            try:
                it["findings"].extend(fut.result())
            except Exception as e:  # noqa: BLE001 — Sicherheitsnetz
                it["findings"].append(_f("unknown", "watch",
                                         f"{r['ref']}: Analyse fehlgeschlagen — {e}"))
    order = {"action": 0, "watch": 1, "info": 2}
    for it in items:
        it["findings"].sort(key=lambda f: order.get(f["severity"], 9))
    return {"generated": NOW.isoformat(), "theme": theme, "items": items}


# ---------------------------------------------------------------- Text-Output

ICON = {"ready_for_you": "🟢", "merged": "✅", "ci_red": "🔴", "comment_unanswered": "💬",
        "review_stale": "⏳", "no_movement": "💤", "closed": "⚪", "unknown": "❓"}


def render_text(res: dict) -> str:
    items = res["items"]
    L: list[str] = []
    L.append(f"Dev-Radar · {res['theme']} · {res['generated'][:16].replace('T', ' ')} UTC")
    L.append("=" * 72)

    def bucket(sev: str) -> list[tuple[dict, dict]]:
        return [(it, f) for it in items for f in it["findings"] if f["severity"] == sev]

    act = bucket("action")
    ready = [(i, f) for i, f in act if f["type"] == "ready_for_you"]
    if ready:
        L.append("\n▶ WARTET AUF DICH")
        for it, f in ready:
            L.append(f"  🟢 {it['title'][:60]}\n     {f['text']}\n     {f['url']}")

    rest = [(i, f) for i, f in act if f["type"] != "ready_for_you"]
    if rest:
        L.append("\n▶ ACTION")
        for it, f in rest:
            L.append(f"  {ICON.get(f['type'], '•')} {it['title'][:60]}\n     {f['text']}")
            if f["url"]:
                L.append(f"     {f['url']}")

    watch = bucket("watch")
    if watch:
        L.append("\n▶ BEOBACHTEN")
        for it, f in watch:
            L.append(f"  {ICON.get(f['type'], '•')} {it['title'][:55]} — {f['text']}")

    quiet = [it for it in items if not it["findings"]]
    if quiet:
        L.append(f"\n▶ RUHIG ({len(quiet)}): " +
                 "; ".join(it["title"][:38] for it in quiet))

    n_ref = sum(1 for it in items for r in it["refs"] if r["host"] != "jira")
    L.append("\n" + "-" * 72)
    L.append(f"{len(items)} Items · {n_ref} Code-Refs geprueft · "
             f"{len(act)} action · {len(watch)} watch")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live-Status der Dev-Items vom To-do-Board")
    ap.add_argument("--file", type=Path, default=DEFAULT_BOARD)
    ap.add_argument("--theme", default=DEFAULT_THEME)
    ap.add_argument("--json", action="store_true", help="maschinenlesbar")
    args = ap.parse_args()
    try:
        res = run(args.file.resolve(), args.theme)
    except Exception as e:  # noqa: BLE001 — immer valides JSON liefern, nie crashen
        res = {"generated": NOW.isoformat(), "theme": args.theme, "items": [],
               "error": f"{type(e).__name__}: {e}"}
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(render_text(res))
        if res.get("error"):
            print(f"\n⚠ Fehler: {res['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
