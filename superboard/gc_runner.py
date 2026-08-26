#!/usr/bin/env python3
"""Board-Agent-Runner — arbeitet ein @gc:-Board-Item mit einer headless
Claude-Code-Instanz ab (Auto-Mode mit Classifier) und schreibt die Antwort
als @gc-re: in den GC-Faden zurück.

Architektur (bewusst Single-Writer): Der Runner fasst board.md NIE direkt an —
jedes Write geht als POST /api/gc-append an den laufenden todo-board-Server
(dort: Lock + lost-Guards + atomic write). Der Server startet run_item() in
einem Daemon-Thread (/api/gc-run); manuell geht auch:

    python3 gc_runner.py --id <gc-id> [--url http://127.0.0.1:47822] [--timeout 900]

Fail gracefully: JEDER Fehlerpfad (Timeout, Crash, kaputtes JSON, Resume weg)
endet als sichtbare ❌-Antwort im Faden — nie stumm. Wenn sogar der Rückpost
scheitert, liegt das Ergebnis als Sidecar in inbox/gc-threads/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import sidecar as _sc  # geteilte Sidecar-Logik (auch server.py/migrate_diet.py) — board.md-Diät 2026-07-16
import git_state as _git  # Kern: Prompt-Gitblock + Faden-Anchor, unabhängig von Receipts
import receipt_hook as _receipt  # optionale Run-Telemetrie; nie Teil des Kernpfads
import thread_search as _thread_search  # read-only Rückgriff auf relevante frühere Fäden

import config as _cfg
import contract as _contract
import paths as _p
from claude_identity import (default_claude_env, identity_for_runner, session_transcript,
                             without_claude_account_env)

GC_ROOT = _p.GC_ROOT
# The workspace copy of the board client (see __main__._bootstrap). Prompts must
# name THIS path, not `python3 -m superboard.board_write`: the agent runs in its own
# shell, which has no guarantee that the server's interpreter or venv is reachable.
BOARD_WRITE = _p.DATA / "board_write.py"
# A source checkout may provide an explicit identity wrapper; normal installations use
# plain `claude`. No ambient binary override: account routing is installation policy.
_PRIVATE_WRAPPER = GC_ROOT / "tools" / "claude-identities" / "claude-private"
PRIVATE_CMD = str(_PRIVATE_WRAPPER) if _PRIVATE_WRAPPER.is_file() else "claude"
SIDECAR_DIR = _p.THREADS
# Run-Journal (Härtung 2026-07-14, „jetzt"): jeder Run hinterlässt hier seine Spur,
# BEVOR die Antwort ins Board appended wird — ein Server-Neustart mitten im Run konnte
# die Antwort sonst still verschlucken (Daemon-Thread stirbt mit dem Prozess).
# Gitignored; erfolgreiche Runs räumen ihr Journal selbst weg.
JOURNAL_DIR = _p.JOURNAL
# Token-Optimierung (2026-07-20, Item 59e9a5c83f24): eine JSONL-Zeile pro Run mit
# Tokens/Cache-Quote/Kosten — Datengrundlage für "wo verbrennen wir das Limit?".
# Gitignored, wächst nur append-only; Auswertung ad hoc (jq/python).
USAGE_LOG = _p.USAGE_LOG
# Prompt-Cache (2026-07-28, dieses Item): Claude Code schreibt den Git-Status in den
# SYSTEM-Prompt und baut ihn bei JEDEM Prozessstart neu — auch beim --resume. Der Cache-Schlüssel
# ist der exakte Prompt-Anfang, also macht jeder Board-Run den Cache seines eigenen Nachfolgers
# ungültig, sobald er seine Sidecar-Datei schreibt oder committet (gemessen: EINE neue Datei
# drückt cache_read von 34.647 auf 17.536 und erzwingt 18.992 neue Tokens). Wir schalten den
# nativen Block ab und hängen die Git-Fakten stattdessen an den PROMPT — der wird hinten
# angefügt und kann den Prefix nicht brechen (gemessen: 792 statt 18.992 neue Tokens, trotz
# geändertem Baum). Herleitung: inbox/analyses/2026-07-28_git-kontext-im-prompt.md
# Cache-TTL (2026-07-30, dieses Item): Claude Code schreibt den Cache per Default mit
# 1-Stunden-TTL — das kostet beim Schreiben 2× Basis-Input statt 1,25× bei 5 Minuten. Der
# Aufpreis kauft Haltbarkeit ÜBER Run-Grenzen. Innerhalb eines Runs reichen 5 Minuten mit
# Reserve — über 68 Runs lag KEIN Abstand zwischen zwei Requests über 5 min (p50 4s, p99 138s,
# max 291s). Nicht verwechseln mit der Gesamtlaufzeit eines Runs: die liegt bei 21% der Runs
# über 5 min, ist aber irrelevant, weil der Cache Request für Request weitergereicht wird.
# 5m bleibt (2026-08-07, Entscheidungsblatt zu Item c074630e8b89; bestätigt am selben Tag
# nach Neumessung). Die Begründung hat sich zweimal geändert, die Zeile nicht:
#   - NICHT mehr gültig: "ein Folge-Run erbt ausnahmslos nur den System-Block (~17k)". 17k ist
#     der MEDIAN. --resume erbt den Cache sehr wohl: 25 von 115 vermessenen resumten Runs (22 %)
#     hatten cache_read > 50k im ersten Request, und genau die kamen in <10 min zurück. Der
#     Prefix bricht nicht, die 5m-TTL läuft nur meistens vorher ab.
#   - NICHT mehr gültig: "1h kostet netto ~34 $/Woche". Die Formel dahinter (W < 1,53*P) zählte
#     den Prefix doppelt — P steckt zu 87 % als cache_creation des ersten Requests schon in W.
# Richtig gerechnet liegt der Break-even bei 70 % Cache-Trefferquote. GEMESSEN wird die nicht
# erreicht: unter 1h trafen 28 % der Folge-Runs (n=65), unter 5m 14 % (n=36) — 1h kostet damit
# ~106k Token pro Run mehr, rund 40 $/Woche. 5m bleibt also, diesmal mit belastbarem Grund.
# Der Bruch liegt NICHT an der TTL-Länge: auch INNERHALB der 5 min trifft nur die Hälfte
# (5/10 Treffer, danach 0/26, Fisher p~0,0007). Warum, ist offen — die Hook-Hypothese und die
# "langer Vorlauf-Run"-Hypothese sind beide widerlegt (§5j). Nächster Schritt wäre ein Proxy-Log
# der ausgehenden Requests inkl. cache_control-Marker; aus den Transkripten allein geht es nicht.
# Aus demselben Grund NICHT gebaut: TTL pro Run/Item umschaltbar (Idee 30.07.) — 1h wäre in
# jeder Stellung die teurere. Der reale Hebel ist die Fadengröße: geerbter Kontext beim Resume
# liegt im Median bei 143k Tokens, und jeder der ~31 internen Turns liest ihn erneut
# (cache_read = 42,5 % der Board-Kosten).
# Herleitung: context/2026-07_prompt-caching-mechanik.md §5j (Regime-getrennte Messung) + §5i + §5h
BASE_ENV = dict(os.environ)
# Identity is a runner invariant, not a property of whichever shell happened to start the
# long-lived Board server. Plain Claude always means the CLI's default account.
RUN_ENV = default_claude_env(
    BASE_ENV,
    CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS="1",
)
# Actively REMOVED, not merely unset: BASE_ENV inherits the board server's own
# environment, so a run started from inside a run would otherwise pass the old
# variable on. Forcing the 5-minute cache bucket was measured to be a bad trade
# for this workload — of 357 resumed runs, only 7 % started warm, because the
# board's own cadence is far longer than five minutes. Without the variable the
# CLI picks its own bucket.
RUN_ENV.pop("FORCE_PROMPT_CACHING_5M", None)
# Bezugspunkt für das Delta im Folge-Turn: {gc_id: {"head": sha, "dirty": [...], "ts": epoch}},
# geschrieben am RUN-ENDE. Gitignored wie das übrige Journal.
GIT_ANCHOR = JOURNAL_DIR / "git-anchor.json"
GIT_ANCHOR_MAX = 300  # Items sind endlich, aber die Datei soll nicht ewig wachsen
GIT_LIST_MAX = 15     # Kappung je Liste — s. git_state.git_facts
DEFAULT_URL = "http://127.0.0.1:47822"

# --- Timeout-Modell (umgestellt 2026-07-27, Entscheidungsblatt Frage 1 = A) ---------------
# ALT: eine harte Stoppuhr (30 min), die feuerte, egal ob der Agent arbeitete oder hing.
# Sie lag mitten in der Normalverteilung — der längste ERFOLGREICHE Lauf im usage-log war
# 29,2 min — und hat am 27.07. acht laufende Runs erschlagen (drei davon in derselben
# Sekunde, aus einer Run-all-Welle). Von außen war „arbeitet noch" nicht von „hängt" zu
# unterscheiden, weil `--output-format json` erst GANZ AM ENDE schreibt: out.json blieb
# 0 Byte, live nachgemessen an vier Runs nach 22–27 min.
# NEU: `--output-format stream-json` liefert eine JSON-Zeile pro Ereignis, also einen
# Herzschlag. Gekillt wird nur noch bei echtem STILLSTAND; die Gesamtdauer ist nur noch
# eine Notbremse gegen Endlosschleifen. Ein arbeitender Agent stirbt nicht mehr an der Uhr,
# ein hängender stirbt schneller als vorher (15 statt 30 min).
DEFAULT_TIMEOUT = 7200  # Notbremse: 120 min Gesamtlaufzeit (hochgesetzt von 60, 18.08.:
# die Stillstandserkennung befristet seitdem auch OFFENE Werkzeuge —
# tote Runs sterben an der Werkzeug-Frist, die Notbremse fängt nur noch fleißige Endlosschleifen)
IDLE_TIMEOUT = int(os.environ.get("GC_IDLE_TIMEOUT", "900"))  # 15 min OHNE ein einziges Ereignis
# Frist für OFFENE Werkzeuge: solange ein Werkzeug läuft, schweigt der Strom komplett
# (gemessen 2026-07-27: `sleep 100` = 110 s lang 0 Byte) — bis 08/2026 ruhte die
# Stillstands-Uhr deshalb unbegrenzt, und ein still hängender MCP-Aufruf überlebte bis
# zur Notbremse. Jetzt gilt: normales Werkzeug 15 min Funkstille (Bash beendet sich
# selbst nach max. 10 min, MCP-Clients haben eigene Timeouts — was dann noch schweigt,
# ist tot), Sub-Agenten 45 min (Deep-Research-Subs arbeiten legitim 20–40 min still).
BUSY_TIMEOUT = int(os.environ.get("GC_BUSY_TIMEOUT", "900"))
BUSY_TIMEOUT_AGENT = int(os.environ.get("GC_BUSY_TIMEOUT_AGENT", "2700"))
# Werkzeuge, hinter denen ein Sub-Agent arbeitet: "Agent" heißt es im Strom der aktuellen
# CLI (gemessen 18.08.), "Task" in älteren; ein externer opencode-artiger Sub ist ein
# BLOCKIERENDER MCP-Aufruf, der bei einer "deep"-Stufe legitim >15 min schweigt. Gemessen
# 18.08. außerdem: ein LAUFENDER Sub-Agent ist im Elternstrom gar nicht still (task_progress
# + innere tool_use-Events) — die 45er-Frist greift also nur, wenn der Sub WIRKLICH tot ist.
AGENT_TOOLS = frozenset({"Task", "Agent", "mcp__sub-agent__run_subagent"})
# Werkzeuge, die ein Board-Run NIE benutzt, deren Schema aber in JEDER Anfrage steckt.
# Gemessen 2026-08-20 am echten Draht (cache-probe/p2-run3/016.req.body, opus-5, turn 1 =
# 145.311 Zeichen = 55.682 Token, kalibriert 2,61 ch/Token):
#   Workflow 8.205 Tok (14,7 % des GESAMTEN Prefix!) · ScheduleWakeup 1.898 · ReportFindings
#   883 · ListAgents 452  →  zusammen 11.438 Token pro Turn, für nichts.
# Erreichbar ist davon nur eins: ScheduleWakeup gehört zu /loop, ReportFindings zu
# /code-review, ListAgents zum Sitzungs-Messaging — ein headless One-Shot hat das nicht.
# `Workflow` dagegen SCHON: Claude Code selbst schreibt in jeden Run-System-Prompt „Do not
# use workflows or deep-research unless the user requested it" (am Draht nachgelesen
# 20.08., nicht aus unserem Kernel), und das Werkzeug nennt headless/cron-Läufe in seiner
# eigenen Beschreibung. Sagt der owner es im Item, wäre der Opt-in also erfüllt — deshalb
# das Profil unten statt eines pauschalen Verbots.
# Kontrollmessung (gleiche Flags, sonnet, Trivial-Prompt): 49.068 → 38.130 Token, also
# −10.938 (−22,3 %). Die Differenz deckt sich mit der Summe der Schemata: --disallowed-tools
# ENTFERNT das Schema, es verbietet nicht bloß den Aufruf.
# Rausnehmen, falls ein Board-Run eines dieser Werkzeuge doch mal braucht.
UNUSED_TOOLS = ("Workflow", "ScheduleWakeup", "ReportFindings", "ListAgents")
# Profile, die `Workflow` zurückholen — Claude Codes deterministischen Multi-Agent-
# Orchestrator (Skript aus phase()/agent()/parallel(), dutzende Subagenten im Hintergrund).
# Warum als Profil und nicht als Automatik: das Schema kostet 8,2k Token in JEDEM Turn,
# gemessen ~10 % der Kosten eines Median-Opus-Runs (usage-log, 200 Runs) — das lohnt nur
# bei einer Aufgabe, die wirklich fächert. Ein headless Run soll dieselbe echte
# Multi-Agent-Orchestrierung bekommen können wie eine interaktive Session, wenn die
# Aufgabe wirklich tiefere, verzweigte Arbeit braucht. EINZELNE Sub-Agenten (`Agent`)
# waren nie abgeschaltet und laufen headless ganz normal — das hier ist die schwere
# Stufe darüber.
WORKFLOW_PROFILES = frozenset({"opus-multi"})
# Das Werkzeug feuert NUR bei ausdrücklichem Opt-in des Nutzers (eigene Regel im Schema:
# „ONLY call this tool when the user has explicitly opted into multi-agent orchestration").
# Die Profilwahl im Board IST dieser Opt-in — der Agent sieht sie aber nur, wenn sie im
# Prompt steht. Englisch wie der übrige Kontrakt.
WORKFLOW_OPT_IN = (
    "\n\nMulti-agent run: the owner picked the “Multi-Agent” profile for this item, "
    "which is their explicit opt-in for multi-agent orchestration — the `Workflow` tool is "
    "available to you. Use it when the task genuinely fans out (scan → parallel work → "
    "verify/synthesise); for a single helper the `Agent` tool stays the cheaper choice. A "
    "workflow runs in the BACKGROUND: stay in the run until its task-notification arrives, "
    "then own the synthesis yourself — the board only ever sees your final message."
)


def disallowed_tools(profile: str) -> tuple[str, ...]:
    """Welche eingebauten Werkzeuge dieser Run gar nicht erst angeboten bekommt.
    --disallowed-tools ENTFERNT das Schema (gemessen), spart also echten Prefix."""
    if profile in WORKFLOW_PROFILES:
        return tuple(t for t in UNUSED_TOOLS if t != "Workflow")
    return UNUSED_TOOLS


def apply_workflow_opt_in(prompt: str, profile: str) -> str:
    """Hängt den Opt-in-Satz an, wenn das Profil Multi-Agent meint. Ans ENDE, damit
    der gecachte Prompt-Präfix unangetastet bleibt."""
    return prompt + WORKFLOW_OPT_IN if profile in WORKFLOW_PROFILES else prompt


POLL_EVERY = 3  # Takt der Laufzeit-Wache: Herzschlag lesen, Stopp-Wunsch prüfen
# Notausgang (Entscheidungsblatt Frage 6 = A): GC_STREAM=0 → zurück auf den alten
# Einzel-JSON-Pfad. Der Parser versteht weiterhin BEIDE Formate, schon weil alte Journale
# recoverbar bleiben müssen.
STREAM = os.environ.get("GC_STREAM", "1") != "0"
# Gekillte Runs kommen hierher, damit man nachsehen kann, WAS da hing („ggf. in
# fehler logs schreiben ... dann können wir ja reviewen wenn was gekilled wird").
KILL_LOG = _p.JOURNAL / "killed-runs.jsonl"
KILL_KEEP = 15  # so viele Ereignisströme gekillter Runs bleiben zur Nachschau liegen
RECOVER_GRACE = 120  # so lange darf ein Journal ohne lebenden Prozess/Output liegen, bevor es als Absturz gilt
INLINE_MAX = _sc.INLINE_MAX  # längere/mehrzeilige Antworten wandern in einen Sidecar (Quelle: sidecar.py)

# Harte Grenzen für den headless Agenten (zusätzlich zum Auto-Mode-Classifier):
# keine Secrets. Deny-Liste, 2026-07.
# personal/ ist seit 2026-07-14 FREI (Read+Write): "das ist unsere UI zu claude code,
# natuerlich auf privates auch" — private Items (Zeugnis, Coaching, Personen) waren sonst
# strukturell unbearbeitbar. Die Bremse ist jetzt eine Kontrakt-Regel im Prompt (neue
# Dateien/Append ja, bestehende private Dateien nur auf explizite Ansage), keine Deny-Regel.
# git push ist seit 2026-07-17 KEINE Deny-Regel mehr (Faden-Ansage: "ich will hier
# git push erlauben, insbesondere wenn es aus der dev session auch explizit hervorgeht") —
# die Bremse ist jetzt die Kontrakt-Regel im Prompt (push nur bei explizitem Faden-Go oder
# explizitem push-Stand aus der Dev-Session; Identität/Remote prüfen, nie force, nie main).
# Offen für später (Todo): Privacy-Modus, der einzelne Items vor dem Agenten versteckt.
AGENT_SETTINGS = json.dumps({"permissions": {"deny": [
    # ~/.secrets is a placeholder — point this at wherever your own credentials live.
    "Read(~/.secrets/**)", "Read(**/.env*)", "Read(**/.env.*)",
]}})

# --- Zweiter Runner: Codex CLI (Phase 1) -------------------------------------------------
# Plan: inbox/analyses/2026-08_codex-runner-PLAN.md. Alles hier steht auf Läufen gegen
# codex-cli 0.147.0-alpha.6.5 vom 11.08.2026, nicht auf Doku:
#   * Die Binary liegt NICHT im PATH, sondern in der ChatGPT.app. Ein blankes `codex`
#     scheitert mit FileNotFoundError — deshalb der volle Pfad als Default.
#   * `--approve-for-me` ist PFLICHT, sobald MCP im Spiel ist: ohne den Flag scheitert
#     jeder MCP-Tool-Call mit `error: "user cancelled MCP tool call"`, auch mit
#     `-c approval_policy=never`. Der Flag verträgt sich NICHT mit `--sandbox` (die CLI
#     bricht mit Argument-Konflikt ab) — er impliziert workspace-write plus automatische
#     Freigabe-Prüfung. Das ist die nächste Entsprechung zum Auto-Mode der Codex-App.
#   * `--ignore-user-config` hält die persönliche ChatGPT-App-Konfiguration draußen (dort
#     hängen die MCP-Server node_repl/computer-use und ein Analytics-Server mit abgelaufenem
#     OAuth-Token, der bei jedem Lauf auf stderr lärmt). Auth bleibt davon unberührt.
#   * Der Prompt geht über STDIN (`exec -`), nicht als Argument: bei Kernel-Länge bricht
#     die CLI mit „unexpected argument" ab. Wir schieben eine Datei auf stdin statt durch
#     eine Pipe zu schreiben — ein 100k-Prompt in eine 64k-Pipe wäre ein Deadlock.
#   * ACHTUNG Trust Boundary: Codex kennt kein Gegenstück zu AGENT_SETTINGS. workspace-write
#     begrenzt nur das SCHREIBEN (cwd + /tmp); LESEN ist überall erlaubt, auch die
#     Credential-Verzeichnisse (s. AGENT_SETTINGS) und .env*. Offene Frage 1 im Plan —
#     solange sie offen ist, ist Codex bewusst nur pro Run wählbar und nie Default.
CODEX_CMD = os.environ.get("GC_RUNNER_CODEX", "/Applications/ChatGPT.app/Contents/Resources/codex")
# Item-Typen des Codex-Ereignisstroms, die als „Werkzeugaufruf" zählen (Schrittzähler und
# Stillstands-Erkennung). `agent_message` und `todo_list` sind KEINE Werkzeuge.
CODEX_TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call",
                              "web_search", "patch_apply"})

# ---------------------------------------------------------------------------------------
# KONTRAKT-DIÄT 2026-08-07 — zwei stehende Normen (Entscheidungsblatt
# tmp/entscheidungen/board-contract-diaet-entscheidung.html, Item „Board contract"):
#
#   1. Der Kontrakt sagt WAS zu tun ist, dieser Kommentar sagt WARUM. Begründung, Historie
#      und Provenienz ändern keine Handlung des Agenten, verwässern aber die Regeln, an
#      denen ein Run wirklich hängt (Push-Gate, personal/, bump.py). AUSNAHME: eine
#      Begründung, die das Modell vor einer wörtlichen Fehlauslegung bewahrt, bleibt im
#      Prompt — sie ist Anweisung, getarnt als Begründung. Deshalb steht „Drei ehrliche
#      Zeilen schlagen sieben pflichtschuldige" weiterhin drin („das Warum kann schon
#      helfen … aber ‚weil es genauso in Claude Code steht' hilft dem Agenten null").
#   2. Was die Root-CLAUDE.md vollständig trägt, steht hier NICHT nochmal. Der Kernel liegt
#      bei jedem Board-Run im (gecachten) System-Prompt. Nur wo es ein echtes Board-Delta
#      gibt, bleibt ein Einzeiler.
#
# Nicht verloren, nur hierher ausgelagert — die 2026-08-07 gestrichenen Begründungen:
#  * „Eine abgeschnittene erste Zeile liest sich als kaputte Antwort": sidecar.py:55-63
#    kappt bereits satzweise; die Regel, die zählt (1 Satz, ≤200 Zeichen), steht davor.
#  * Secrets-Zeile („kein Zugriff auf die Credential-Verzeichnisse und .env*"): technisch
#    erzwungen von AGENT_SETTINGS oben, und PROMPT_REMINDER wiederholt sie ohnehin.
#  * ···-Split: ohne Split wird jedes body-reiche Item zur Textwand in der Matrix-Übersicht
#    — genau das soll die Übersicht vermeiden.
#  * Push-Historie: gelockert 2026-07-17 auf Ansage im 4892-Faden („ich will
#    hier git push erlauben, insbesondere wenn es aus der dev session hervorgeht"), vorher
#    pauschal »NIEMALS git push«.
#  * bump.py: kein Git-Hook kann das übernehmen — gemessen 23.07., `pre-commit` kennt die
#    Commit-Message nicht, `commit-msg` kann nicht mehr stagen.
#  * Arbeitsstand-Zweck: der owner soll den Faden jederzeit schließen können, ohne dass
#    Wissen verloren geht — nicht Vollständigkeit. Beim Abhaken wandert der Block ins
#    Rohlager (logs/dreaming/arbeitsstand-archiv.md) und verschwindet aus dem Item.
#  * Git-Handwerk-Provenienz: übernommen aus Claude Codes eingebautem Hinweis-Block, der
#    für Board-Runs abgeschaltet ist (er würde bei jedem Run den Prompt-Cache brechen).
#    Bewusst NICHT übernommen: „nur committen, wenn gefragt" und „auf dem Default-Branch
#    erst branchen" — die Commit-/Push-Regeln im Kontrakt sind die strengere Quelle.
#  * Operator-Mindset + Learning Capture: die Substanz steht vollständig im Kernel
#    („Task-model pairing", „Learning Capture Protocol") — im Kontrakt bleibt das Delta.
#
# Bewusst NICHT gekürzt: der Entscheidungsblatt-Bullet (Q4 = „unverändert lassen").
# Der Inbox-Absatz ist seit dieser Runde bedingt → _inbox_hint(), er ging vorher an 100 %
# der Runs, obwohl er nur für Items im Thema „Inbox" gilt.
#
# ACHTUNG: Die gerenderten Verträge sind MODUL-KONSTANTEN — Änderungen in contract.py
# ODER board.contract.md greifen erst nach einem server process restart. The cockpit warns
# when loaded Python code is stale.
# ---------------------------------------------------------------------------------------
PROMPT_CONTRACT = _contract.render("full")
PROMPT_REMINDER = _contract.render("reminder")


def _contract_for(runner: str, variant: str = "full") -> str:
    """Kontrakt mit dem CLI-Handoff-Befehl des RICHTIGEN Runners (Phase 7).

    Der Kontrakt-Text nennt für session-gebundene Auth-Handoffs wörtlich
    `claude --resume <SESSION>` — ein Codex-Agent würde diesen Befehl brav in seine
    Handoff-Antwort schreiben, obwohl seine Session damit nicht erreichbar ist.
    Ersetzt wird der Laufzeit-String, nicht die Konstante: Claude bleibt der Default.
    CODEX_CMD steht mit vollem Pfad im Text, weil `codex` nicht im PATH liegt."""
    base = PROMPT_CONTRACT if variant == "full" else PROMPT_REMINDER
    if runner != "codex":
        return base.replace("`claude --resume <SESSION>`",
                            f"`{PRIVATE_CMD} --resume <SESSION>`")
    return base.replace(
        "`claude --resume <SESSION>` + `!<auth-cmd>`",
        f"`{CODEX_CMD} resume <SESSION>` and run the authentication command there",
    ).replace("with your actual session UUID", "with your actual thread ID")


# Handoff-Hinweis (2026-07-22, Blatt e67ba06428b7: Q1=B Agent beurteilt selbst,
# Q2=D Schwelle 200k, Q4=A leichter Ton — "hey, Handoff könnte gerade gut sein", kein
# Zwang, kein Widerspruchs-Ritual).
#
# WARUM SO KLEIN: Die Messung am 22.07. hat die ursprüngliche Annahme widerlegt. Der
# komplette Fresh-Prompt des LÄNGSTEN Fadens auf dem Board (52 Turns) ist ~3.760 Token —
# davon Faden-Text nur ~1.280. Seit der Sidecar-Diät stehen ältere Turns als Kurzzeile
# im Prompt, nicht als Volltext. Der Faden ist also NICHT der Kostentreiber; teuer ist
# das Claude-Code-Session-Transkript (alte Tool-Ergebnisse, Datei-Reads, Sub-Outputs),
# das --resume mitschleppt: Resume-Run Ø $7.72 vs. Fresh-Run Ø $3.43.
# Folge: Ein separates Handoff-Dokument spart praktisch nichts — der SCHNITT ist der
# ganze Hebel, und der ist heute schon möglich und billig. Was vom Schneiden abhält,
# ist Angst vor Wissensverlust. Der Arbeitsstand kauft also Mut zum Schnitt, nicht Token.
# Deshalb: kein neuer Marker, keine neue Datei, kein Endpoint — nur ein Block im
# Item-Body, der bei frischen Runs ohnehin schon im Prompt landet.
HANDOFF_HINT_TOKENS = 200_000
_CTX_K_RE = re.compile(r"~(\d+)k")

PROMPT_HANDOFF_HINT = """\

NOTE — this session carried ~{k}k context in its previous run. Two things before you finish:
1. Check the item's `### Working state` block (contract rule above): does it describe the CURRENT \
state, or an obsolete one? When the budget is exceeded, CONDENSE it; do not externalize it.
2. Tell the owner in ONE sentence whether this is a good cut point now ("✂ New thread" means the \
next run starts with a fresh session) — and if not, why not. A cut is cheap: the fresh prompt costs \
only ~4k tokens, while the working state carries the status. Cut at conceptual boundaries \
(milestone complete, new kind of work, old tool results irrelevant to the next phase), not by number."""


def _handoff_hint(gc_last: str) -> str:
    """Hinweis nur oberhalb der Schwelle — darunter kostet das Feature null Token.
    Quelle ist der @gc-last-Stempel des VORHERIGEN Runs, also eine Untergrenze:
    der laufende Run ist bereits größer. Bewusst akzeptiert (ab 200k soll er
    "darüber nachdenken", Richtung 300-400k wird es kritisch)."""
    m = _CTX_K_RE.search(gc_last or "")
    if not m:
        return ""
    k = int(m.group(1))
    return PROMPT_HANDOFF_HINT.format(k=k) if k * 1000 >= HANDOFF_HINT_TOKENS else ""


def _slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:32] or "item"


SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")  # UUID-artig; alles andere resumen wir nicht


def session_uuid(session: str) -> str:
    """Kanonischer Resume-Handle = alles vor dem optionalen ' · label'.
    Nicht-UUID-artiges (Hand-Edit, Müll) → leer = frische Session statt CLI-Fehler."""
    handle = session.split(" · ")[0].strip()
    return handle if SESSION_ID_RE.match(handle) else ""


def session_runner(session: str) -> str:
    """Welcher Runner hat diese Session erzeugt? Steht als letztes Segment im Label
    (`<uuid> · board-slug · codex`). Claude-Zeilen bleiben bewusst unverändert — kein
    Migrationsschritt für alle bestehenden Fäden, und ein fehlendes Segment ist der
    Default. Ein Handle der einen CLI ist für die andere wertlos: er sähe zwar wie eine
    gültige UUID aus, führt aber in einen Session-Fehler."""
    s = session.strip()
    return "codex" if s.endswith(" · codex") else "claude"


def _resume_handle_lives(resume_id: str, runner: str) -> bool:
    """Return whether a Claude resume transcript exists in the default store.

    Codex has its own store and layout, so its handle is left to the CLI. A missing
    Claude transcript starts fresh with the full thread text instead of failing before
    the first turn.
    """
    if runner == "codex":
        return True
    try:
        return session_transcript(runner, resume_id, GC_ROOT) is not None
    except Exception:  # noqa: BLE001 — eine Pfadprüfung darf keinen Run verhindern
        return True


def session_cut(turns: list[dict]) -> bool:
    """Steht ein @gc-done: NACH der letzten Antwort, ist die gespeicherte Session tot:
    der nächste Run startet FRISCH statt --resume. „Faden schließen" ist damit unser
    Context-Management-Hebel (2026-07-14) — sonst wächst eine Board-Session
    endlos weiter. Verloren geht nichts: build_prompt(resume=False) legt den kompletten
    Faden (auch die Turns vor dem Schnitt) als Text in den Prompt.
    Warum „nach der letzten Antwort" und nicht „irgendwo im Faden": nach dem frischen
    Run steht das alte @gc-done: ja immer noch da — sonst würde INNERHALB des neuen
    Abschnitts nie wieder resumt."""
    last_reply = max((i for i, e in enumerate(turns) if e["kind"] == "reply"), default=-1)
    return any(e["kind"] == "done" and i > last_reply for i, e in enumerate(turns))


def _looks_like_dead_session(out: dict) -> bool:
    """Heuristik: schlug der Run fehl, WEIL die Session weg ist (→ frisch neu ok)?
    Timeout/Crash zählen nicht — da könnte schon gearbeitet worden sein. Kontingent-
    Fehler auch nicht: „You've hit your session limit" enthält zwar „session", ein
    Retry liefe aber sofort in dieselbe Wand (beobachtet 16.08.2026)."""
    blob = (out.get("raw_error", "") + " " + str(out.get("reply", ""))).lower()
    if "no conversation found" in blob:  # eindeutig: der Handle zeigt ins Leere
        return True
    if any(k in blob for k in ("timeout", "session limit", "usage limit", "out of usage")):
        return False
    return any(k in blob for k in ("session", "resume", "conversation"))


def _looks_like_quota_exhausted(out: dict) -> bool:
    """Heuristik: starb der Run daran, dass das ABO-KONTINGENT des Seats leer ist?
    Nur dann lohnt der Seat-Fallback (s. run_item) — bei jedem anderen Fehler würde der
    zweite Seat exakt denselben Fehler nochmal produzieren, nur teurer.

    Bewusst eng formuliert: die Phrasen stammen aus der CLI-Meldung („Claude AI usage
    limit reached") bzw. aus dem `rate_limit_event` des Streams. Ein einfaches „429"
    reicht NICHT — das ist die normale Drosselung eines Werkzeugs (z. B. der Teams-
    Connector) und hat mit dem Seat-Kontingent nichts zu tun."""
    blob = " ".join(str(out.get(k, "")) for k in ("raw_error", "reply")).lower()
    if any(p in blob for p in ("usage limit", "quota", "kontingent", "limit reached",
                               "upgrade to increase")):
        return True
    status = str((out.get("beat") or {}).get("rate_limit", "")).lower()
    return status in ("rejected", "exhausted", "blocked")


def _expand_ask(text: str, sidecar_dir: Path | None) -> str:
    """Lazy-Loading-Ausnahme (Diät-Runde 2): der AKTUELLE Arbeitsauftrag wird
    voll expandiert — der Agent darf nie mit nur einem Satz des Auftrags losarbeiten.
    Ältere Turns bleiben Kurzzeile+Pfad (liest er selbst bei Bedarf, s. Kontrakt).
    Fail gracefully: fehlende Sidecar-Datei → Kurzzeile + sichtbarer Marker."""
    if not _sc.REF_RE.search(text):
        return text
    full = _sc.expand(text, sidecar_dir)
    if full is None:
        return f"{text} (Sidecar is missing — full text not found)"
    return f"{text}\n[Full text of the externalized task:]\n{full}"


THREAD_TAIL_TURNS = 30  # Leak 4: frische Runs auf langen Fäden — nur der jüngste Teil als Text

STAGE_VOCAB_HINT = "plan → rfc → approved → wip → review → tested → deployed"


def _stage_hint(pending: dict, runner: str = "claude") -> str:
    """Phase 3 (stage-tags-PLAN.md, Q5): sanfter Prozess-Stups — NUR für Dev-Items
    (Thema beginnt mit „dev", wie isDevTheme im Frontend). Zeigt dem Agenten die
    aktuelle Stufe und bittet, abgeschlossene Schritte per @stage:-Zeile ans Item zu
    hängen; ohne Tag zusätzlich der Planning-Hinweis. Reiner Hinweis, blockiert nichts.
    Nicht-Dev-Items bekommen einen Leerstring. Failsafe: nie eine Exception."""
    try:
        name = (pending.get("addr") or {}).get("name", "") or ""
        if not re.match(r"dev\b", name, re.I):
            return ""
        gc_id = str((pending.get("addr") or {}).get("id", "")).strip()
        fmt = ("When you complete a process step, append it through "
               f"`python3 {BOARD_WRITE} --id {gc_id or '<gc-id>'} "
               "--stage '<stage> · <path-or-note> *(YYYY-MM-DD)*'` (deliberately skipped: "
               "`<stage> · skip: <reason> *(…)*`). Never edit board.md for a stage. "
               f"Stages: {STAGE_VOCAB_HINT}.")
        stages = pending.get("stages") or []
        if stages:
            cur = stages[-1] if isinstance(stages[-1], dict) else {}
            when = f" (last {cur.get('date')})" if cur.get("date") else ""
            return f"\n\nProcess stage of this development item: **{cur.get('stage', '?')}**{when}. {fmt}"
        plan_method = (
            "planning tools (master plan)"
            if runner == "claude"
            else "repo-native master-plan artifact using the local planning conventions"
        )
        return ("\n\nThis development item has NO process stage yet. Before substantive development, "
                "decide whether the work is genuinely multi-phase: several stages, files, or "
                "systems where early findings can redirect later work. If yes, create a "
                f"plan with the {plan_method} and tag it as `@stage: plan · <plan-path> *(date)*`. "
                "If it is a linear handful of steps, record "
                "`plan · skip: linear task; plan lives in this thread *(date)*` and proceed. "
                f"{fmt}")
    except Exception:
        return ""


def _body_write_hint(pending: dict) -> str:
    """Item-specific address + revision for stale-safe body replacements.

    The static contract can prescribe the path, but only the freshly read pending
    entry knows the revision. This block therefore goes into both Fresh and Resume;
    a Resume can be days old, so its body snapshot is not reliable right now.
    """
    try:
        gc_id = str((pending.get("addr") or {}).get("id", "")).strip()
        body_etag = str(pending.get("body_etag") or "").strip()
        if not gc_id or not body_etag:
            return ""
        return ("\n\nTo replace this item's BODY safely, write the new body to a file and run "
                f"`python3 {BOARD_WRITE} --id {gc_id} --body-file <path> "
                f"--body-etag {body_etag}`. Never edit board.md for this. HTTP 409 means the "
                "body changed after this run started: use `--show`, merge the current body, and retry.")
    except Exception:
        return ""


def _inbox_hint(pending: dict) -> str:
    """Bedingter Block (Kontrakt-Diät 2026-08-07, Q5=A): die Anleitung „gib dem Item
    einen echten Titel und verschieb es" ging bisher als fester Kontrakt-Bullet an 100 % der
    Runs, obwohl sie NUR für Items im Landeplatz-Thema „Inbox" gilt (zum Messzeitpunkt lag
    davon kein einziges auf dem Board). Die Bedingung ist exakt die, unter der die Regel
    überhaupt greift — sie kann also nicht ausgerechnet dann fehlen, wenn sie zählt.

    Fünfter Vertreter des Musters (neben _stage_hint, _hierarchy_block, Handoff-Hinweis,
    _git_context) und damit der erste konkrete Schritt Richtung „Kontrakt pro Task
    assemblieren". Nur im Fresh-Zweig: im Resume kennt die Session ihn schon (Diät-Logik).
    Failsafe: nie eine Exception."""
    try:
        name = ((pending.get("addr") or {}).get("name", "") or "").strip()
        if not re.match(r"inbox\b", name, re.I):
            return ""
        return ("\n\nThis item is in the 'Inbox' theme — the quick-capture landing zone, not a real "
                "theme; its title is only the start of the captured text. Give it a proper title "
                "and move it by editing `inbox/board.md` into an existing theme/column when one "
                "clearly fits (change the checkbox line and title, but leave `@gc-id:`, "
                "`@gc-session:`, and thread lines below it unchanged — your reply addresses the "
                "item by ID, not by theme/title, so it remains stable after the move). If nothing "
                "clearly fits, leave it in Inbox; do not force it.")
    except Exception:  # noqa: BLE001 — ein Prompt-Zusatz ist nie einen Abbruch wert
        return ""


def _hierarchy_block(pending: dict, resume: bool) -> str:
    """Der eigentliche Mehrwert hierarchischer Items: der Agent trägt den Kontext entlang
    der `@gc-parent`-Kante — runter beim Öffnen eines Sub-Fadens, hoch als Statuszeile in
    JEDEM Eltern-Turn (Design-Entscheidung ④). Geht in beide Prompt-Zweige (fresh + resume): der Sub-Stand
    ändert sich zwischen den Turns, ein einmalig gesendeter Block wäre sofort veraltet.

    EHRLICHE GRENZE, bewusst akzeptiert (Design-Abnahme 23.07.): „letzte 3 Turns"
    bevorzugt Aktualität vor Relevanz. Ältere Entscheidungen/Constraints des Elternfadens
    fallen raus — deshalb steht der Ausschnitt-Hinweis samt Nachlade-Pfad IM Block, statt
    so zu tun, als wäre das der ganze Kontext."""
    h = pending.get("hierarchy") or {}
    out = []
    if par := h.get("parent"):
        turns = par.get("turns") or []
        lines = [f"  [{_cfg.OWNER if t['kind'] == 'ask' else _cfg.AGENT}] {t.get('text', '')}" for t in turns]
        more = max(0, int(par.get("total_turns") or 0) - len(turns))
        out.append(
            f"\n\nSUB-THREAD — this item belongs to a parent item:\n"
            f"'{par.get('title', '')}' (@gc-id {par.get('id', '')})\n"
            + (f"Excerpt from the parent thread (the latest {len(lines)} turns"
               + (f", {more} older omitted" if more else "") + "):\n" + "\n".join(lines)
               if lines else "The parent thread has no turns yet.")
            + "\nThis is an EXCERPT, not the entire context. If something is missing "
              "(an earlier decision or constraint), read the parent item in `inbox/board.md` "
              "or its sidecars in `inbox/gc-threads/` before assuming. Your reply belongs in "
              "THIS thread; completing it automatically adds a roll-up line to the parent item.")
    if (subs := h.get("subs")) is not None and subs:
        rows = []
        for s in subs:
            state = "✓ done" if s.get("done") else {
                "for_gc": "⏳ waiting for GC", "for_owner": "→ waiting for the owner",
                "closed": "✓ thread closed", "none": "· no thread yet"}.get(s.get("status"), "·")
            rows.append(f"  {state} · {s.get('title', '')} (@gc-id {s.get('id', '')})"
                        + (f" — {s.get('result')}" if s.get("done") and s.get("result") else ""))
        done_n = sum(1 for s in subs if s.get("done"))
        out.append(f"\n\nSUB-THREADS of this item ({done_n}/{len(subs)} done):\n" + "\n".join(rows)
                   + ("\nAll sub-threads are done. Check whether the parent item is now complete and "
                      "SUGGEST that the owner check it off; do not check it off yourself."
                      if done_n == len(subs) else ""))
    # Der Spawn-Hinweis ist Kontrakt-Wissen, kein Zustand: im Resume-Zweig kennt die Session
    # ihn schon (Kontrakt-Diät) — dort kostet er nur Token. Der Sub-STATUS oben geht dagegen
    # bei jedem Turn mit, weil er sich zwischen den Turns ändert.
    if not resume and not h.get("parent"):
        out.append(
            "\n\nIf this task splits into several independent subtasks (different owner, wait, or "
            "completion), you may split them into sub-threads — sparingly, not by default. As a "
            "rule, use them only for truly separate workstreams, never for checklist steps; use "
            "sub-`[ ]` bullets for those, also sparingly and only for work that is important and "
            "not obvious. Do not restate the item or record steps implied by the next action. "
            "They appear collapsed behind the card's ☑ badge, so this is not free storage; large "
            "topics become sub-threads. Command:\n"
            "  curl -s localhost:47822/api/gc-spawn-sub -H 'Content-Type: application/json' "
            f"-d '{{\"parent_id\":\"{(pending.get('addr') or {}).get('id', '')}\","
            "\"title\":\"…\",\"ask\":\"<task for the sub-thread>\"}'\n"
            "The sub-thread is a normal board item with its own thread. There is only ONE level "
            "(sub-threads have no sub-threads). Mention in your reply what you split off.")
    return "".join(out)


def _anchor_load() -> dict:
    try:
        return json.loads(GIT_ANCHOR.read_text())
    except Exception:  # noqa: BLE001 — fehlender/kaputter Anker heißt nur: voller Schnappschuss
        return {}


def _anchor_save(gc_id: str, snap: dict) -> None:
    """Arbeitsbaum-Zustand am RUN-ENDE festhalten — der Bezugspunkt des nächsten Turns."""
    data = _anchor_load()
    data[gc_id] = {**snap, "ts": time.time()}
    if len(data) > GIT_ANCHOR_MAX:  # ältester zuerst raus
        for k, _ in sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[:len(data) - GIT_ANCHOR_MAX]:
            data.pop(k, None)
    try:
        GIT_ANCHOR.parent.mkdir(parents=True, exist_ok=True)
        GIT_ANCHOR.write_text(json.dumps(data, indent=1))
    except Exception:  # noqa: BLE001 — Observability ist nie einen Abbruch wert
        pass


def _clip(items: list[str], indent: str = "  ") -> list[str]:
    out = [f"{indent}{x}" for x in items[:GIT_LIST_MAX]]
    if len(items) > GIT_LIST_MAX:
        out.append(f"{indent}… +{len(items) - GIT_LIST_MAX} more")
    return out


def _git_context(gc_id: str, resume: bool) -> str:
    """Der Git-Kontext als PROMPT-Text statt im System-Prompt (s. RUN_ENV).

    Frischer Run: voller Schnappschuss im Format des nativen Blocks — Entscheidung 28.07.:
    „erstmal an dem orientieren, wie es gerade ist". Folge-Run: nur das Delta gegen das Ende
    des letzten Runs DIESES Items.

    Die Beschriftung des Deltas ist bewusst vorsichtig: zwischen zwei Turns schreiben auch
    andere Board-Sessions in dieses Repo. „Seit deinem letzten Turn passiert" ist ehrlich,
    „das hast du getan" wäre es nicht.
    """
    anchor = _anchor_load().get(gc_id) if resume else None
    if anchor:
        d = _git.git_delta(anchor)
        commits, dirty_new = d.get("commits") or [], d.get("dirty_new") or []
        if not commits and not dirty_new:
            return "\n\n## Git\nUnchanged since your previous turn."
        lines = ["\n\n## Git", "Changes in the repository since your previous turn — multiple board "
                 "sessions work here in parallel, so not everything is yours:"]
        if commits:
            lines += [f"New commits ({len(commits)}):", *_clip(commits)]
        if dirty_new:
            lines += [f"Newly open files ({len(dirty_new)}):", *_clip(dirty_new)]
        return "\n".join(lines)
    f = _git.git_facts(GIT_LIST_MAX)
    lines = ["\n\n## Git", "Snapshot from the start of this run — it does not update; check with "
             "`git status` yourself if needed.",
             f"Branch: {f['branch'] or '?'}"]
    if f["status"]:
        more = f" (… +{f['status_more']} more)" if f["status_more"] else ""
        lines += [f"Status{more}:", *[f"  {ln}" for ln in f["status"]]]
    else:
        lines.append("Status: clean")
    if f["commits"]:
        lines += ["Latest commits:", *[f"  {c}" for c in f["commits"]]]
    return "\n".join(lines)


def _kernel_block() -> str:
    """Kernel-Volltext als Prompt-Präfix für Codex-Runs (Phase 3 des Codex-Plans).

    Claude lädt `CLAUDE.md` selbst über den CLAUDE.md-Mechanismus; Codex kennt den nicht
    und würde ihn — wenn überhaupt — per Tool-Call nachlesen. Gemessen am 11.08.: Kernel
    im Prompt = 26.654 Input-Tokens, Kernel per Stub-Hinweis + Nachlesen = 80.787. Der
    direkte Weg ist also nicht nur verlässlicher, sondern dreimal billiger.

    Nur im FRESH-Zweig: eine Resume-Session hat den Block schon gesehen; ihn erneut zu
    schicken wäre der teuerste denkbare Reminder. Fehlt die Datei, liefert die Funktion
    einen leeren String — ein Run stirbt nicht an einem fehlenden Präfix.

    Die Workspace-Kette wird bewusst NUR benannt, nicht mitgeliefert: Codex findet sie
    nachweislich nicht von selbst, holt sie aber zuverlässig, wenn der Pfad im Prompt steht.
    """
    try:
        kernel = (GC_ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return (
        "## Operating instructions (already loaded — DO NOT read again)\n"
        "Below is the complete content of `CLAUDE.md`, the kernel of this repository at the "
        "start of this run. Treat it like a system instruction. DO NOT read the file again with "
        "a tool; you already have its complete contents here.\n\n"
        f"<kernel file=\"CLAUDE.md\">\n{kernel}\n</kernel>\n\n"
        "The workspace-specific `CLAUDE.md` files named in the kernel are NOT included "
        "here. As soon as your task touches one of those topics, read the matching file yourself "
        "before your first substantive statement, not afterwards.\n\n"
        "---\n\n")


def build_prompt(pending: dict, resume: bool, sidecar_dir: Path | None = None,
                 runner: str = "claude", retrieved_context: str = "") -> str:
    """Erst-Run: volle Item-Beschreibung + Faden. Resume: nur der neue Turn —
    die Session hat den Kontext schon. Ausgelagerte Turns (Sidecar-Verweise)
    bleiben Kurzzeilen — NUR der neueste @gc:-Auftrag wird voll expandiert.

    `retrigger_note`: gesetzt vom Auto-Retrigger (server._maybe_retrigger,
    2026-07-15), wenn diese Nachricht reinkam, während der VORIGE Run für dasselbe
    Item noch lief — der hat sie nie gesehen, sie landet aber trotzdem vor seiner
    Antwort im Faden. Steht am Prompt-Anfang, in BEIDEN Zweigen (resume und fresh),
    damit der Hinweis nicht vom Session-Zustand abhängt."""
    addr = pending.get("addr", {})
    turns = pending.get("thread", [])
    last_ask = pending.get("last_ask") or (turns[-1]["text"] if turns else "")
    last_ask = _expand_ask(last_ask, sidecar_dir)
    note = f"{pending['retrigger_note']}\n\n" if pending.get('retrigger_note') else ""
    if resume:
        # Radar replies happen OUTSIDE the stored CLI session. A resume doesn't know about
        # them even though they're already in the board thread. Carry the radar turns since
        # the previous ask explicitly for this one round; after that they age out. Applies
        # to native replies and to the sys safety-net path alike.
        ask_i = [i for i, e in enumerate(turns) if e.get("kind") == "ask"]
        since = ask_i[-2] + 1 if len(ask_i) > 1 else 0
        radar = [e.get("text", "") for e in turns[since:]
                 if e.get("kind") in ("reply", "sys")
                 and e.get("text", "").startswith("📡 Radar ·")]
        radar_context = ("External radar update(s) added outside this CLI session:\n"
                         + "\n".join(f"- {text}" for text in radar) + "\n\n") if radar else ""
        # Kontrakt-Diät: Session kennt den Voll-Kontrakt schon → Kurz-Reminder reicht.
        # Nach Board-Compact (⚙) einmalig wieder voll — @gc-last trägt dann "kompaktiert…"
        # und wird erst vom nächsten erfolgreichen Run überstempelt.
        compacted = (pending.get("gc_last") or "").startswith("kompaktiert")
        contract = _contract_for(runner, "full" if compacted else "reminder")
        return (f"{note}Continue the board thread '{pending.get('title', '')}'. "
                f"{radar_context}New turn from the owner:\n"
                f"{last_ask}\n\n{contract}{_handoff_hint(pending.get('gc_last', ''))}"
                f"{_body_write_hint(pending)}{_stage_hint(pending, runner)}{_hierarchy_block(pending, resume=True)}"
                f"{_git_context(addr.get('id', ''), resume=True)}{retrieved_context}")
    where = f"{addr.get('name', '')}" + (f" / {addr.get('col')}" if addr.get("col") else "")
    body = "\n".join(pending.get("body", []))
    last_ask_i = max((i for i, e in enumerate(turns) if e["kind"] == "ask"), default=-1)
    # Leak 4 (Token-Optimierungs-Faden 2026-07-21): frische Runs auf langen Fäden bekamen
    # die KOMPLETTE Historie als Text — bei 40+ Turns 1,5-3k Tokens, resident bei jedem Turn.
    # Nur der jüngste Teil trägt; Älteres steht bei Bedarf in inbox/board.md.
    # sys-Turns (Sub-Roll-up) gehören in den Prompt, auch wenn die UI sie nach 2h ausblendet:
    # „das wird weiterhin mitgesendet … der Agent kann das zusammenhalten mit der ID und dem
    # Namen" (23.07.). Nach dem Archivieren eines Subs ist diese Zeile die einzige Spur.
    entries = [(i, e) for i, e in enumerate(turns) if e["kind"] in ("ask", "reply", "sys")]
    dropped = len(entries) - THREAD_TAIL_TURNS
    if dropped > 0:
        entries = entries[-THREAD_TAIL_TURNS:]
    lines = [
        f"[{ {'ask': _cfg.OWNER, 'reply': 'You (earlier)', 'sys': 'System'}[e['kind']] }] "
        + (_expand_ask(e.get("text", ""), sidecar_dir) if i == last_ask_i else e.get("text", ""))
        for i, e in entries]
    if dropped > 0:
        lines.insert(0, f"[… {dropped} older turns omitted — read the item in inbox/board.md if needed]")
    thread_txt = "\n".join(lines)
    # Carry-over (17.08., Blatt auto-run-needs-input Q3=C „nur warnen, nie blocken"):
    # der ▶ hat einen Run gestartet, obwohl die letzte Antwort noch auf den Input des owners
    # wartet. Gesetzt NUR von server.run_cockpit_action; ▶ wischt die Session, also reicht
    # der Fresh-Zweig. Der neue Run muss die offene Entscheidung sichtbar weitertragen —
    # der owner schaut nur auf das jeweils letzte Blatt/die letzte Antwort.
    carry = ""
    if pending.get("carryover"):
        carry = ("⚠ CARRY-OVER: the previous round's reply still awaits "
                 f"{_cfg.OWNER}'s input ({pending['carryover']}) and this new round was "
                 f"started anyway. {_cfg.OWNER} only looks at the LATEST reply and its "
                 "sheet. If this run ends in a decision sheet, fold the still-open "
                 "questions of the previous round into it (marked as carried over from "
                 "the previous round); otherwise state in the first lines of your reply "
                 "that the previous question/sheet is still open. A pending decision "
                 "must never silently disappear.\n\n")
    # Kernel-Präfix nur für Codex und nur beim frischen Turn — s. _kernel_block().
    kernel = _kernel_block() if runner == "codex" else ""
    platform = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}.get(runner, runner)
    return (f"{kernel}{note}You are the Superboard Agent (S-Agent, headless run started by Superboard).\n"
            f"Current agent platform: {platform}. This successful run proves this platform is already working; do not ask the user to set it up again.\n"
            f"Board item: '{pending.get('title', '')}' ({where})\n"
            + (f"Item notes:\n{body}\n" if body else "")
            + f"\nBoard thread so far:\n{thread_txt}\n\n"
            f"{carry}Task: Handle the latest [{_cfg.OWNER}] turn.\n\n{_contract_for(runner)}"
            f"{_body_write_hint(pending)}{_stage_hint(pending, runner)}{_inbox_hint(pending)}"
            f"{_hierarchy_block(pending, resume=False)}"
            f"{_git_context(addr.get('id', ''), resume=False)}{retrieved_context}")
            # Kein Handoff-Hinweis im Fresh-Zweig (Fehlfeuer, gefunden 2026-07-22 im eigenen
            # Prompt dieses Items): @gc-last beschreibt die ALTE, gerade geschnittene Session.
            # Eine frische Session trägt ~0k — der Hinweis behauptete "~225k" und fragte nach
            # einem Schnittpunkt direkt NACH dem Schnitt. Fresh == neue Session, immer.


def _context_tokens(env: dict) -> int:
    """Kontextgröße des Runs aus dem usage-Block: input + cache_read + cache_creation
    der LETZTEN Iteration (≈ was beim nächsten --resume wieder auf dem Tisch liegt).
    Top-Level-usage summiert über alle Iterationen — das würde bei Multi-Turn-Runs
    überzählen; iterations[-1] ist der ehrliche Stand. 0 = nicht ermittelbar."""
    usage = env.get("usage") or {}
    iters = usage.get("iterations") or []
    u = iters[-1] if isinstance(iters, list) and iters else usage
    try:
        return sum(int(u.get(k) or 0) for k in
                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    except (TypeError, ValueError):
        return 0


def _usage_summary(env: dict, main_model: str = "") -> dict:
    """Kosten-/Cache-Sicht eines Runs aus dem claude-JSON-Envelope für usage-log.jsonl.
    Nutzt Top-Level-usage (Summe über ALLE Iterationen) — für Verbrauch/Kosten richtig,
    im Gegensatz zu _context_tokens, das bewusst nur die letzte Iteration nimmt.

    ACHTUNG, der zentrale Fund (gemessen 2026-08-07, Item „Subagenten-Kosten sichtbar
    machen", kontrollierter Zweimodell-Lauf gegen claude 2.1.x):

      * Top-Level `usage` zählt NUR den Haupt-Agenten. Sub-Agenten fehlen dort komplett.
      * `modelUsage` zählt ALLES — Haupt-Agent UND Subs, aufgeschlüsselt pro Modell.
      * Summe der `costUSD` über `modelUsage` == `total_cost_usd` (auf den Cent).

    Genau daran ist die erste Kostenanalyse gescheitert: Token-Felder gegen `cost_usd`
    gehalten fehlten ~27 % (die Subs). Deshalb loggen wir jetzt `tokens_by_model` und
    die abgeleitete Differenz `sub_tokens` = Σ(modelUsage) − Top-Level-usage. Die ist
    exakt und modellunabhängig — sie sieht auch einen Sub, der auf DEMSELBEN Modell wie
    der Haupt-Agent lief. `sub_cost_usd` kann das nicht: Kosten lassen sich innerhalb
    eines Modell-Eimers nicht aufteilen, deshalb ist das eine UNTERGRENZE (alles, was
    nicht auf dem Hauptmodell lief). Bei nicht ermittelbarem Hauptmodell: None.
    Die bestehenden Felder (input_tokens, cache_read, …) bleiben bewusst Haupt-Agent-only,
    damit alte Log-Zeilen vergleichbar bleiben — neu ist ausschliesslich, was dazukommt.
    """
    u = env.get("usage") or {}

    def n(k: str) -> int:
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    read, create, inp = n("cache_read_input_tokens"), n("cache_creation_input_tokens"), n("input_tokens")
    total_in = read + create + inp
    # modelUsage: {"claude-opus-5": {"costUSD": 0.88, ...}, "claude-sonnet-5": {...}} — der
    # claude-CLI-Envelope traegt das schon pro Modell, wir warfen bisher nur die Keys raus
    # (sorted .keys()) und liessen costUSD liegen. Ohne das laesst sich "wie viel von den
    # Run-Kosten kam vom teuren Hauptmodell vs. den Sonnet-Subs" nie ohne neuen Full-Scan
    # nachmessen (Token-Audit 9eea34ffe7c2, 2026-08-07, Blatt-Frage 1 = A "ja einbauen").
    model_usage = {m: v for m, v in (env.get("modelUsage") or {}).items() if isinstance(v, dict)}
    cost_by_model = {m: round(v["costUSD"], 4) for m, v in model_usage.items()
                      if isinstance(v.get("costUSD"), (int, float))}

    # Pro Modell: alles, was gelaufen ist (Haupt-Agent + Subs auf diesem Modell).
    felder = (("in", "inputTokens"), ("out", "outputTokens"),
              ("cr", "cacheReadInputTokens"), ("cw", "cacheCreationInputTokens"))

    def z(v: dict, k: str) -> int:
        try:
            return int(v.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    tokens_by_model = {m: {kurz: z(v, lang) for kurz, lang in felder} for m, v in model_usage.items()}
    haupt = {"in": inp, "out": n("output_tokens"), "cr": read, "cw": create}
    sub_tokens = {k: sum(t[k] for t in tokens_by_model.values()) - haupt[k] for k in haupt}
    # Negativ kann nur heissen: die beiden Zählwege sind auseinandergelaufen (anderes
    # CLI-Format). Dann lieber nichts behaupten als eine falsche Zahl loggen.
    if any(v < 0 for v in sub_tokens.values()):
        sub_tokens = {}
    sub_cost = (round(sum(c for m, c in cost_by_model.items() if m != main_model), 4)
                if main_model and cost_by_model else None)

    return {"input_tokens": inp, "cache_read": read, "cache_creation": create,
            "output_tokens": n("output_tokens"),
            "cache_hit_pct": round(100 * read / total_in) if total_in else None,
            "cost_usd": env.get("total_cost_usd"), "duration_ms": env.get("duration_ms"),
            "num_turns": env.get("num_turns"),
            "models": sorted(model_usage.keys()),
            "cost_by_model": cost_by_model,
            "main_model": main_model,
            "tokens_by_model": tokens_by_model,
            "sub_tokens": sub_tokens,
            "sub_cost_usd_min": sub_cost}


def _erster_turn_cache(out_path: Path | None) -> dict:
    """Cache-Bilanz des ERSTEN Modell-Aufrufs eines Runs — die einzige Zahl, die zeigt,
    ob der Cache ÜBER Run-Grenzen hinweg trägt.

    Warum nicht `cache_hit_pct`: das misst über den ganzen Run und liegt fast immer bei
    ~96 %, weil jeder Werkzeug-Turn den bisherigen Verlauf aus dem Cache liest. Das ist
    Within-Run-Caching und wäre auch bei null Wiederverwendung hoch — es hat uns 2026-08
    einmal glauben lassen, das Cache-Thema sei erledigt. Cross-Run sichtbar wird allein an
    Turn 1: liest der ~den ganzen Kontext (gut) oder schreibt er ihn neu (Präfix gebrochen)?

    `ttl` protokolliert zusätzlich, welche Cache-Lebensdauer der Server vergeben hat.
    Das wählen wir NICHT — es hängt an einem serverseitigen Feature-Gate; die Env-Schalter
    ENABLE_PROMPT_CACHING_1H / FORCE_PROMPT_CACHING_5M blieben im Test 2026-08-10 auf -p
    wirkungslos. Wir halten es fest, um zu sehen, wann es kippt, statt es zu vermuten.
    """
    if not out_path:
        return {}
    try:
        with open(out_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                u = (d.get("message") or {}).get("usage") or {}
                if not u.get("cache_creation") and not u.get("cache_read_input_tokens"):
                    continue
                cc = u.get("cache_creation") or {}
                f5, f1 = int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)
                return {"t1_read": int(u.get("cache_read_input_tokens") or 0),
                        "t1_write": int(u.get("cache_creation_input_tokens") or 0),
                        # ungecachter Rest von Turn 1 (meist nur die neue Nachricht). Erst
                        # damit ist read/(read+write+input) ein ehrlicher Nenner statt einer
                        # Quote über zwei von drei Posten.
                        "t1_input": int(u.get("input_tokens") or 0),
                        "ttl": "1h" if f1 > f5 else ("5m" if f5 else None)}
    except OSError:
        pass
    return {}


def log_usage(gc_id: str, title: str, model: str, resumed: bool, out: dict,
              log_path: Path | None = None, out_path: Path | None = None) -> None:
    """Eine JSONL-Zeile pro Run. Reines Reporting — darf einen Run NIE brechen."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "gc_id": gc_id, "title": title[:80],
               "model": model or "(default)", "resumed": resumed, "ok": out.get("ok", False),
               "identity": identity_for_runner(runner_of(model)),
               **(out.get("usage_summary") or {}), **_erster_turn_cache(out_path)}
        if out.get("thread_context"):
            rec["thread_context"] = out["thread_context"]
        with open(log_path or USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — Reporting-Pfad, bewusst still
        pass


def _lesbarer_rest(roh: str | None, grenze: int = 240) -> str:
    """Letzte verwertbare Zeile eines fehlgeschlagenen Laufs — ohne JSON-Trümmer.

    Der Fehlertext landet als Faden-Turn im Board und damit vor den Augen des owners.
    Roh durchgereicht war das bisher der abgeschnittene Schwanz eines Ereignis-Stroms,
    also Zeug wie `…userModified":false,"replaceAll":false}}` — technisch korrekt und
    für einen Menschen wertlos. Wir werfen deshalb JSON-Zeilen weg und behalten die
    letzte echte Klartext-Zeile; bleibt keine übrig, sagen wir das ehrlich.
    """
    zeilen = [z.strip() for z in (roh or "").strip().splitlines() if z.strip()]
    for z in reversed(zeilen):
        if z.startswith(("{", "[")) or z.endswith(("}", "]")):
            continue
        return z[-grenze:]
    return "no plain text in the error stream (only stream fragments)"


def _envelope(stdout: str) -> tuple[dict | None, str, str]:
    """stdout → (Ergebnis-Envelope, session_id, Hauptmodell). Versteht BEIDE Ausgabeformate:

    * `--output-format json` (alt): genau ein JSON-Objekt, das ganz am Ende geschrieben wird.
    * `--output-format stream-json` (neu): eine JSON-Zeile pro Ereignis; das Schluss-Event
      `{"type":"result",…}` trägt exakt dieselben Felder wie der alte Envelope (gegen
      claude 2.1.220 verifiziert) — deshalb bleibt alles dahinter unverändert.

    Die session_id kommt notfalls aus dem ERSTEN Ereignis (`system/init`). Das ist der
    Grund, warum ein abgebrochener Run künftig fortsetzbar ist: bis 2026-07-27 hieß es im
    Faden „Session ist evtl. resumebar", gespeichert wurde aber nichts — beim Timeout gab
    es kein geparstes JSON, also blieb session_id leer und der Handle war weg.
    Beide Formate müssen unterstützt bleiben: die Journal-Recovery liest auch Ströme, die
    ein alter Serverprozess geschrieben hat.

    Das Hauptmodell steht NUR im `system/init`-Ereignis (das Schluss-Event trägt es nicht,
    gegen claude 2.1.x geprüft) — im alten Einzelobjekt-Format also gar nicht, dann "".
    Gebraucht wird es für die Sub-Kosten-Aufteilung in `_usage_summary`.
    """
    text = (stdout or "").strip()
    if not text:
        return None, "", ""
    try:
        env = json.loads(text)  # Einzelobjekt = altes Format
        # ... ABER nur, wenn es auch ein Ergebnis ist. Ein Strom, der nach genau einer
        # Zeile abbrach, ist ein gültiges Einzelobjekt (das init-Ereignis) — als Envelope
        # gelesen postete die Recovery dafür „subtype=init" statt „kein Ergebnis".
        if isinstance(env, dict) and env.get("type") in (None, "result"):
            return env, str(env.get("session_id", "")), ""
        if isinstance(env, dict):
            return None, str(env.get("session_id", "")), str(env.get("model", "") or "")
        return None, "", ""
    except (json.JSONDecodeError, ValueError):
        pass
    env, sid, haupt = None, "", ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # halb geschriebene Schlusszeile eines gekillten Runs — kein Grund zu scheitern
        if not isinstance(ev, dict):
            continue
        if not sid and ev.get("session_id"):
            sid = str(ev["session_id"])
        if not haupt and ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("model"):
            haupt = str(ev["model"])
        if ev.get("type") == "result":
            env = ev  # letztes result gewinnt
    return env, sid, haupt


def _parse_claude_stdout(stdout: str, stderr: str, returncode: int | None) -> dict:
    """claude-Ausgabe → Outcome-Dict. Gemeinsamer Parser für den Live-Pfad
    (spawn_claude) und die Journal-Recovery (Server-Neustart mitten im Run)."""
    env, sid_hint, haupt_modell = _envelope(stdout)
    if env is None:
        return {"ok": False, "reply": "", "session_id": sid_hint, "denials": [],
                "context_tokens": 0, "usage_summary": {},
                "raw_error": f"no result from Claude (exit {returncode}): "
                             f"{_lesbarer_rest(stderr or stdout)}"}
    if env.get("is_error") or env.get("subtype") != "success":
        # `errors` MIT in den raw_error: bei subtype=error_during_execution ist `result`
        # leer, der Grund steht nur dort ("No conversation found with session ID: …").
        # Ohne diese Zeile sah _looks_like_dead_session() nur den nichtssagenden
        # Subtype — der Frisch-Retry lief nie an und der Turn war weg (16./17.08.2026).
        grund = "; ".join(str(e) for e in (env.get("errors") or []) if e)
        return {"ok": False, "reply": str(env.get("result", "")), "session_id": env.get("session_id", "") or sid_hint,
                "denials": env.get("permission_denials", []), "context_tokens": 0,
                "usage_summary": _usage_summary(env, haupt_modell),
                "raw_error": f"is_error={bool(env.get('is_error'))} "
                             f"subtype={env.get('subtype')}{': ' + grund if grund else ''}"}
    return {"ok": True, "runner": "claude", "reply": str(env.get("result", "")).strip(),
            "session_id": env.get("session_id", "") or sid_hint,
            "denials": env.get("permission_denials", []), "context_tokens": _context_tokens(env),
            "usage_summary": _usage_summary(env, haupt_modell), "raw_error": ""}


class StreamTail:
    """Liest den Ereignisstrom INKREMENTELL mit und hält daraus den Live-Zustand.

    Warum inkrementell: der Strom eines langen Runs wird viele MB groß (gemessen: ~4 KB
    pro Ereignis, weil Werkzeug-Ergebnisse mit drinstehen). Alle drei Sekunden die ganze
    Datei neu zu parsen wäre nach einer halben Stunde absurd — also merken wir uns den
    Byte-Offset und lesen nur den Rest.

    Die letzte Zeile eines Lesevorgangs kann halb geschrieben sein (der Kindprozess
    schreibt ja weiter, während wir lesen); sie bleibt im Puffer liegen, bis sie
    vollständig ist. Ohne das würde jeder zweite Lesevorgang eine kaputte Zeile sehen.

    „Schritte" statt „Turns": `num_turns` steht NUR im Schluss-Event. Eine live
    hochgezählte Turn-Zahl würde am Ende von der echten abweichen — wir zählen deshalb
    Werkzeugaufrufe, die sind eindeutig und sagen genau das, was man wissen will.
    """

    def __init__(self, path: Path) -> None:
        self.path, self.offset, self._buf = path, 0, ""
        # `_open` = laufende Werkzeugaufrufe (tool_use ohne zugehöriges tool_result),
        # id → Werkzeugname. SCHLÜSSELSTELLE, gemessen 2026-07-27: während ein Werkzeug
        # arbeitet, ist der Ereignisstrom KOMPLETT still — ein `sleep 100` im Bash-Tool
        # erzeugte 110 s lang exakt 0 Byte. „Datei wächst nicht" heißt also NICHT „Agent
        # hängt". Ohne diese Buchführung hätte der neue Leerlauf-Timeout genau den Fehler
        # reproduziert, den er beseitigen sollte: einen arbeitenden Agenten an der Uhr
        # erschlagen. Der NAME (statt nur der Menge) steckt drin, weil die Frist fürs
        # Schweigen vom Werkzeug abhängt — Sub-Agenten dürfen länger (busy_budget).
        self._open: dict[str, str] = {}
        self.state: dict = {"session_id": "", "steps": 0, "last_tool": "", "rate_limit": "",
                            "busy": 0, "busy_tool": "", "workflow": False}
        # Optionaler Abnehmer für jedes GEPARSTE Ereignis (SSE-Endpoint im Server).
        # Als Hook statt Rückgabewert: watch_run interessiert nur der Zustand, und die
        # Zeile hier ist billiger als ein zweiter Parser mit eigener Offset-Buchführung.
        self.on_event = None

    def poll(self) -> dict:
        try:
            with open(self.path) as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return self.state  # Datei weggeräumt (Journal-Wache) — kein Grund, den Run zu töten
        if chunk:
            self._buf += chunk
            lines = self._buf.split("\n")
            self._buf = lines.pop()  # Rest = evtl. halbe Zeile, beim nächsten Mal komplett
            for line in lines:
                self._absorb(line)
        return self.state

    def _absorb(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(ev, dict):
            return
        if not self.state["session_id"] and ev.get("session_id"):
            self.state["session_id"] = str(ev["session_id"])
        if ev.get("type") == "rate_limit_event":
            status = str(ev.get("rate_limit_info", {}).get("status", ""))
            # nur Auffälliges merken — „allowed" ist der Normalfall und wäre Rauschen
            self.state["rate_limit"] = "" if status in ("", "allowed") else status
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.state["steps"] += 1
                    self.state["last_tool"] = str(block.get("name", ""))
                    self._open[str(block.get("id", ""))] = str(block.get("name", ""))
                    self.state["busy_tool"] = str(block.get("name", ""))
                    # Ein Workflow kehrt SOFORT zurück (Task-ID) und arbeitet dann im
                    # Hintergrund weiter: kein offenes Werkzeug, trotzdem darf der Strom
                    # lange schweigen. Ab dem ersten Workflow gilt deshalb auch für den
                    # werkzeuglosen Stillstand die Sub-Agenten-Frist.
                    if self.state["last_tool"] == "Workflow":
                        self.state["workflow"] = True
        if ev.get("type") == "user":
            content = ev.get("message", {}).get("content", [])
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._open.pop(str(block.get("tool_use_id", "")), None)
        self.state["busy"] = len(self._open)
        if not self._open:
            self.state["busy_tool"] = ""
        if self.on_event is not None:
            self.on_event(ev)

    def busy_budget(self) -> int:
        """Wie lange der Strom bei OFFENEM Werkzeug schweigen darf, bevor wir von einem
        Hänger ausgehen (18.08.: „nichts passiert INKLUSIVE kein offener Sub-Agent →
        stoppen"). Sub-Agenten bekommen die lange Frist; bei parallel offenen Werkzeugen
        zählt die großzügigste — ein hängendes Bash neben einem arbeitenden Sub darf den
        Sub nicht mit in den Tod reißen."""
        if any(name in AGENT_TOOLS for name in self._open.values()):
            return BUSY_TIMEOUT_AGENT
        return BUSY_TIMEOUT


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal an die PROZESSGRUPPE des Runs, mit Rückfall auf den Einzelprozess.

    Warum die Gruppe: `claude` startet selbst Prozesse (Bash-Tool, MCP-Server). Ein Signal
    nur an claude lässt genau die am Leben — ein Build oder eine Testsuite liefe fröhlich
    weiter, während das Board „⏹ Von dir gestoppt" anzeigt. Der Stopp-Knopf gäbe ein
    Versprechen, das er nicht hält. Deshalb spawnt spawn_claude mit `start_new_session=True`
    (eigene Gruppe) und wir signalisieren die ganze Gruppe.
    Restrisiko bleibt: ein Kind, das selbst `setsid` macht, entkommt trotzdem.

    LEBENSWICHTIGE Bremse: nur signalisieren, wenn die Gruppe des Kindes NICHT unsere
    eigene ist. Ohne diesen Vergleich schießt ein Kill die eigene Prozessgruppe ab — also
    den Board-Server selbst. Genau das ist beim ersten Testlauf passiert (der Test hat
    sich wortlos mitsamt Shell beendet); in Produktion hätte ein Druck auf den
    Stopp-Knopf das Board mitgerissen. `start_new_session=True` beim Spawn ist die
    Voraussetzung, dieser Check ist die Absicherung, falls sie mal fehlt."""
    try:
        pgid = os.getpgid(proc.pid)
        if pgid != os.getpgid(0):
            os.killpg(pgid, sig)
            return
    except (OSError, ProcessLookupError):
        pass
    try:  # eigene Gruppe, Gruppe schon weg, oder kein POSIX → nur den Prozess selbst
        proc.send_signal(sig)
    except (OSError, ProcessLookupError, ValueError):
        pass


def _kill_proc(proc: subprocess.Popen) -> None:
    """Erst höflich (SIGTERM), dann bestimmt (SIGKILL). claude räumt bei SIGTERM seine
    Session sauber ab; erst wenn es das nicht tut, gehen wir hart drauf."""
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(10)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass


def watch_run(proc: subprocess.Popen, tail: StreamTail | None, hard_cap: int,
              stop_path: Path | None = None, on_beat=None) -> tuple[str, float]:
    """Bewacht den laufenden Agenten bis zum Ende. Gibt (kill_grund, laufzeit) zurück;
    kill_grund == "" heißt: normal fertig geworden.

    Gekillt wird NUR bei
      * "idle"  — IDLE_TIMEOUT lang kein Ereignis UND kein Werkzeug in Arbeit,
      * "hung"  — Werkzeug offen, aber busy_budget lang kein Ereignis (Werkzeug hängt),
      * "cap"   — Notbremse gegen Endlosschleifen,
      * "stop"  — der owner hat den Stopp-Knopf gedrückt (Datei stop_path taucht auf).

    Das „und kein Werkzeug in Arbeit" ist keine Feinheit, sondern trägt die ganze Idee:
    zwischen tool_use und tool_result schweigt der Strom vollständig (gemessen: 110 s
    Funkstille bei einem `sleep 100`). Ein lang laufendes Werkzeug — Testsuite, Build,
    MCP-Aufruf, langsame Netz-Anfrage — sähe sonst exakt wie ein Hänger aus, und der neue
    Timeout würde arbeitende Runs killen wie der alte. Bis 08/2026 ruhte die Uhr bei
    offenem Werkzeug deshalb UNBEGRENZT — der Preis: ein still hängender Aufruf überlebte
    bis zur Notbremse, und die musste dafür eng bleiben (60 min). Seit dem Umbau (18.08.)
    ist auch die Werkzeug-Wartezeit befristet (busy_budget: 15 min normal, 45 min
    Sub-Agenten) — dadurch durfte die Notbremse auf 120 min steigen und fängt nur noch
    den einen Fall, den keine Stillstandsuhr sieht: die FLEISSIGE Endlosschleife.

    Der Stopp läuft bewusst über eine DATEI statt über einen direkten kill() aus dem
    Server heraus: sonst sieht diese Wache nur „Prozess plötzlich tot" und würde einen
    gewollten Abbruch als Absturz in den Faden schreiben. So kennt der, der killt, auch
    den Grund. Preis: bis zu POLL_EVERY Sekunden Verzögerung am Knopf.

    Ohne Ereignisstrom (GC_STREAM=0) gibt es keinen Stillstandsbegriff — dann greift nur
    die Notbremse, also faktisch das alte Verhalten mit größerem Limit.
    """
    started = last_event = time.time()
    last_size = 0
    while True:
        try:
            proc.wait(POLL_EVERY)
            return "", time.time() - started
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if stop_path is not None and stop_path.exists():
            _kill_proc(proc)
            return "stop", now - started
        if tail is not None:
            try:
                size = tail.path.stat().st_size
            except OSError:
                size = last_size
            if size > last_size:
                last_size = size
                last_event = now
                state = tail.poll()
                if on_beat:
                    try:
                        on_beat({**state, "last_event": now, "elapsed": now - started})
                    except Exception:  # noqa: BLE001 — Observability kostet nie einen Run
                        pass
            # Werkzeug in Arbeit → längere, aber ENDLICHE Frist (busy_budget statt
            # IDLE_TIMEOUT). Der Zustand kommt aus dem zuletzt gelesenen Strom; er bleibt
            # korrekt, auch wenn seither nichts mehr kam — genau dann ist er entscheidend.
            silent = now - last_event
            if tail.state.get("busy"):
                if silent >= tail.busy_budget():
                    _kill_proc(proc)
                    return "hung", now - started
            elif silent >= (BUSY_TIMEOUT_AGENT if tail.state.get("workflow") else IDLE_TIMEOUT):
                _kill_proc(proc)
                return "idle", now - started
        if now - started >= hard_cap:
            _kill_proc(proc)
            return "cap", now - started


def _kill_outcome(reason: str, elapsed: float, state: dict, hard_cap: int) -> dict:
    """Abbruch → Outcome-Dict. Wichtig ist die session_id aus dem Strom: damit steht der
    Resume-Handle im Faden und der Run lässt sich WIRKLICH fortsetzen."""
    mins = int(elapsed // 60)
    if reason == "stop":
        head = f"⏹ Stopped by you after {mins} min"
    elif reason == "idle":
        head = f"❌ Aborted after {mins} min: no activity for {IDLE_TIMEOUT // 60} min (stalled)"
    elif reason == "hung":
        # Das Budget hier aus busy_tool rekonstruiert (bei parallelen Werkzeugen eine
        # Näherung — genau genug für die Meldung, der Kill selbst nutzt busy_budget).
        budget = BUSY_TIMEOUT_AGENT if state.get("busy_tool") in AGENT_TOOLS else BUSY_TIMEOUT
        head = (f"❌ Aborted after {mins} min: {state.get('busy_tool') or 'a tool'} produced "
                f"no output for {budget // 60} min (likely hung)")
    else:
        head = f"❌ Safety stop: total runtime reached {hard_cap // 60} min"
        # Die Wache pollt; schläft der Mac dazwischen, kann die echte Laufzeit weit über
        # der Kappe liegen (gemessen 29.07.: Kappe 60 min, Abbruch nach 100,7 min). Die
        # Meldung nannte dann nur die Kappe — wer den Lauf später prüft, rechnet mit der
        # falschen Zahl. Weicht es spürbar ab, steht die echte Zeit dabei.
        if mins >= hard_cap // 60 + 2:
            head += f" (actually {mins} min — the watcher polls; the computer likely slept)"
    if state.get("steps"):
        head += f" — {state['steps']} steps, last {state.get('last_tool') or '?'}"
    if state.get("busy") and reason == "cap":
        # Der aufschlussreichste Fall: nicht der Agent hing, sondern ein Werkzeug kam nie
        # zurück. Ohne diesen Hinweis sucht man den Fehler an der falschen Stelle.
        head += f" · last stuck in {state.get('busy_tool') or 'a tool'}"
    if state.get("rate_limit"):
        head += f" · Rate-Limit: {state['rate_limit']}"
    sid = state.get("session_id", "")
    head += " · session saved; the next message continues there" if sid \
        else " · no session handle in the stream; the next run starts fresh"
    return {"ok": False, "reply": "", "session_id": sid, "denials": [], "context_tokens": 0,
            "usage_summary": {}, "raw_error": head, "killed": reason, "elapsed": elapsed,
            "beat": dict(state)}


def log_kill(gc_id: str, title: str, model: str, reason: str, elapsed: float,
             state: dict, out_path: Path | None) -> None:
    """Gekillten Run protokollieren UND seinen Ereignisstrom aufheben („ggf. in
    fehler logs schreiben ... dann können wir ja reviewen wenn was gekilled wird").
    Ohne das Aufheben wäre die Spur weg — das Journal wird nach dem Posten abgeräumt.
    Best effort: ein Fehler hier darf den Run nicht mitreißen."""
    try:
        KILL_LOG.parent.mkdir(parents=True, exist_ok=True)
        kept = ""
        if out_path and out_path.exists():
            keep_dir = KILL_LOG.parent / "killed"
            keep_dir.mkdir(parents=True, exist_ok=True)
            dest = keep_dir / f"{out_path.name.split('.')[0]}.{reason}.jsonl"
            dest.write_bytes(out_path.read_bytes())
            kept = str(dest)
            old = sorted(keep_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)[:-KILL_KEEP]
            for p in old:
                p.unlink(missing_ok=True)
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "gc_id": gc_id, "title": title,
               "model": model or "default", "reason": reason, "elapsed_min": round(elapsed / 60, 1),
               "steps": state.get("steps", 0), "last_tool": state.get("last_tool", ""),
               "rate_limit": state.get("rate_limit", ""), "session_id": state.get("session_id", ""),
               "stream": kept}
        with open(KILL_LOG, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"gc_runner: Kill-Log fehlgeschlagen: {e}", file=sys.stderr)


def prompt_files(prompt_dir: Path, gc_id: str) -> list[Path]:
    """Prompt-Mitschnitte eines Items, AELTESTER zuerst — die eine Sortier-Quelle.

    NICHT nach Dateiname sortieren: der Name ist
    `run-<gc_id>-<YYYYmmdd-HHMMSS>-<zufall4hex>.prompt.txt`. Zwei Runs in DERSELBEN
    Sekunde (Run-All, Auto-Retrigger) unterscheiden sich dann nur noch im
    Zufallssuffix — `sorted()` ordnet nach Zufall statt nach Zeit. Folgen: die
    Retention in save_prompt() loescht `[:-PROMPT_KEEP]` und erwischt dabei den
    NEUESTEN Mitschnitt, und die Anzeige (`/api/prompt`) nimmt `hits[-1]` und zeigt
    einen beliebigen der gleichsekuendigen Runs. Real als 20-%-Flake im Test
    test_prompt_mitschnitt_ueberlebt_discard sichtbar geworden (2026-07-22).

    mtime ist die echte Schreibreihenfolge; der Name bleibt als Tiebreak fuer den
    Fall gleicher mtime (grobe Dateisystem-Aufloesung).
    """
    if not prompt_dir.is_dir():
        return []
    return sorted(prompt_dir.glob(f"run-{gc_id}-*.prompt.txt"),
                  key=lambda p: (p.stat().st_mtime, p.name))


class RunJournal:
    """Durabilität für einen Board-Agent-Run: Meta + claude-stdout liegen auf Platte,
    bevor irgendetwas appended wird. Lebenszyklus: running (Meta + pid, stdout fließt
    in .out.json) → ready (fertiger Antworttext im Meta) → nach erfolgreichem
    gc-append gelöscht. Was diesen Weg nicht zu Ende geht, trägt recover_journals()
    beim nächsten Serverstart nach — der Neustart-Pfad war stiller Verlust (2026-07-14)."""

    def __init__(self, gc_id: str, title: str, base_url: str, timeout: int,
                 model: str = "", journal_dir: Path | None = None) -> None:
        # None statt Default-Bindung an JOURNAL_DIR: Tests biegen gc_runner.JOURNAL_DIR auf
        # ein Temp-Dir um — ein zur def-Zeit gebundener Default würde das ignorieren, und
        # die Journal-Wache des LIVE-Servers erntete/löschte Test-Journale mitten im Lauf
        # (cross-Prozess-Race, real als Test-Flake gesehen 2026-07-16).
        journal_dir = journal_dir or JOURNAL_DIR
        journal_dir.mkdir(parents=True, exist_ok=True)
        name = f"run-{gc_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.meta_path = journal_dir / f"{name}.meta.json"
        self.out_path = journal_dir / f"{name}.out.json"
        self.err_path = journal_dir / f"{name}.err.txt"
        # Stopp-Wunsch (der owner-„esc", 2026-07-27): der Server legt diese Datei an,
        # die Laufzeit-Wache sieht sie und killt SELBST — so kennt der Killende den Grund
        # und ein gewollter Abbruch landet nicht als „Absturz" im Faden.
        self.stop_path = journal_dir / f"{name}.stop"
        self.meta = {"gc_id": gc_id, "title": title, "base_url": base_url,
                     "timeout": timeout, "model": model, "started": time.time(),
                     "pid": None, "status": "running", "reply_text": "", "session": "",
                     "gc_last": "", "beat": {}}
        self._write()
        self._last_beat = 0.0

    def _write(self) -> None:
        tmp = self.meta_path.with_suffix(".tmp")  # atomar — halbes Meta wäre nicht recoverbar
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=1))
        tmp.replace(self.meta_path)

    def set_pid(self, pid: int) -> None:
        self.meta["pid"] = pid
        self._write()

    BEAT_EVERY = 10  # Meta nicht bei jedem Poll neu schreiben — der Server hört ohnehin am Callback

    def beat(self, state: dict) -> None:
        """Lebenszeichen ins Meta. Zweitweg neben dem on_beat-Callback: dadurch ist der
        Zustand eines Runs auch von AUSSEN sichtbar (CLI-Run, anderer Serverprozess,
        Nachschau nach einem Absturz)."""
        now = time.time()
        if now - self._last_beat < self.BEAT_EVERY:
            return
        self._last_beat = now
        self.meta["beat"] = {**state, "at": now}
        self._write()

    def mark_killed(self, reason: str, elapsed: float, state: dict) -> None:
        self.meta.update(status="killed", kill_reason=reason, elapsed=elapsed,
                         beat={**state, "at": time.time()})
        self._write()

    def ready(self, reply_text: str, session: str, gc_last: str = "") -> None:
        """Der fertige @gc-re-Text steht ab jetzt auf Platte — der Append darf scheitern."""
        self.meta.update(status="ready", reply_text=reply_text, session=session, gc_last=gc_last)
        self._write()

    # Prompt-Mitschnitt (2026-07-22: „ich will sehen, was angehängt wurde"). Bewusst
    # NEBEN dem Journal, nicht darin: discard() räumt das Journal nach erfolgreichem
    # Append weg — der Prompt wäre exakt in dem Moment weg, in dem der owner nachschaut.
    # Retention: die letzten PROMPT_KEEP je Item, der Rest fliegt beim Schreiben raus.
    PROMPT_KEEP = 3

    def save_prompt(self, prompt: str) -> None:
        """Best effort — ein Fehler hier darf niemals einen Run kosten."""
        try:
            d = self.meta_path.parent / "prompts"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{self.meta_path.name.split('.')[0]}.prompt.txt").write_text(prompt)
            old = prompt_files(d, self.meta["gc_id"])[:-self.PROMPT_KEEP]
            for p in old:
                p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — Observability ist nie einen Abbruch wert
            pass

    def discard(self) -> None:
        for p in (self.meta_path, self.out_path, self.err_path, self.stop_path):
            p.unlink(missing_ok=True)


# Lauf-Profile: EIN Wähler im Board, zwei Flags am Prozess. Der Effort (wie lange das
# Modell vor der Antwort denkt) hängt fest am Modell und ist bewusst KEIN eigener Regler
# (2026-07-27, Faden 49b4eee7d503: "kein Regler, nur der feste Default" — zwei
# Dropdowns wären zwei Entscheidungen pro Run, und Runs löst er im Sekundentakt aus).
# Die Zuordnung ist seine: das teure Modell denkt normal, die günstigen denken länger —
# Denkzeit kompensiert Modellstärke und kostet dabei fast nichts (gemessen 2026-07-27,
# `context/2026-07_effort-level-messung.md`: +62 % Denk-Tokens = +17 % Kosten, weil der
# effort-unabhängige Cache-/Input-Sockel je Run dominiert; teuer ist nur Wall-clock).
# Opus steht doppelt drin — "opus" ist der Alltag, "opus-xhigh" die bewusste Eskalation.
# Der Profilname ist zugleich der Wert im Board-Dropdown und im Usage-Log.
RUN_PROFILES: dict[str, tuple[str, str]] = {
    # Profil        Modell-Alias  Effort
    "":            ("",          "high"),    # kein Modell gewählt (Alt-localStorage, API ohne model)
    "opus":        ("opus",      "medium"),
    "opus-xhigh":  ("opus",      "xhigh"),
    # Multi-Agent: dasselbe Modell wie opus-xhigh, aber MIT dem Workflow-Werkzeug
    # (s. WORKFLOW_PROFILES). Kein eigenes Modell, eine eigene Werkzeugkiste.
    "opus-multi":  ("opus",      "xhigh"),
    "sonnet":      ("sonnet",    "xhigh"),
    # Fable runs medium, not high: in practice this profile is the REVIEW pass (a second
    # look at someone else's work), not the producer — a cheaper entry point rarely costs
    # more, and it means the profile gets reached for more often.
    "fable":       ("fable",     "medium"),
    "haiku":       ("haiku",     "xhigh"),
    # --- Codex-Runner. Der Effort geht nicht über ein Flag, sondern über
    # `-c model_reasoning_effort=…` — s. _codex_argv.
    # Das Modell steht seit 2026-08-12 AUSGESCHRIEBEN da (Faden 42046cf553fc). Ein
    # leerer Alias lief zwar faktisch auch auf gpt-5.6-sol (am Rollout-Log verifiziert),
    # aber das war der CLI-Default — und dessen Rangfolge kommt vom OpenAI-Server, kann
    # sich also mit dem nächsten Update lautlos verschieben. Anders als bei Claude, wo
    # "opus"/"sonnet" gleitende Namen fürs jeweils Neueste sind, ist die Versionsnummer
    # hier Teil des Slugs: ein Sol-Nachfolger heißt anders und muss hier eingetragen
    # werden. Das ist Absicht — nachgesehen wird beim Hygiene-Pass (dort als Punkt
    # hinterlegt), nicht dem Zufall überlassen.
    # Stufen: medium = Alltag, xhigh = Eskalation (analog Opus). "ultra" ist bei Sol
    # nicht bloß mehr Denkzeit, sondern "maximum reasoning with automatic task
    # delegation" — Codex verteilt selbst Teilaufgaben. Laufzeit/Verbrauch im headless
    # Runner sind unerprobt; im Dropdown trotzdem gewünscht ("bei einem würdigen
    # Task ausprobieren"). Alle drei Stufen sind gegen die CLI verifiziert (Rollout-Log
    # zeigt effort xhigh bzw. ultra).
    "codex":        ("gpt-5.6-sol", "medium"),
    "codex-xhigh":  ("gpt-5.6-sol", "xhigh"),
    "codex-ultra":  ("gpt-5.6-sol", "ultra"),
    # Alt-Name aus der Zeit vor dem xhigh-Umstieg. Steht NICHT mehr im Dropdown, bleibt
    # aber gültig: in localStorage hängt er pro Item, und ohne diesen Eintrag fiele so
    # ein Item auf das Default-Profil zurück — also still von Codex zurück auf Claude.
    "codex-high":   ("gpt-5.6-sol", "xhigh"),
}

# Welche Profile welchen Runner meinen. Absichtlich eine eigene Menge statt eines dritten
# Tupel-Elements in RUN_PROFILES: das Tupel wird an mehreren Stellen entpackt, und ein
# zweiter Runner ist kein Grund, jede davon anzufassen.
CODEX_PROFILES = frozenset({"codex", "codex-xhigh", "codex-ultra", "codex-high"})


def runner_of(profile: str) -> str:
    """Profilname → welcher Runner ihn ausführt. Unbekanntes ist Claude (der Default).
    Zwei Werte: "codex" (andere CLI) oder "claude" (der Default)."""
    if profile in CODEX_PROFILES:
        return "codex"
    return "claude"


def resolve_profile(profile: str) -> tuple[str, str]:
    """Profilname → (Modell-Alias, Effort). Unbekanntes fällt auf das Default-Profil
    zurück statt zu werfen: ein Run darf an einem schiefen Dropdown-Wert nicht sterben.
    Die Whitelist-Prüfung passiert vorher im Server (dort mit 400 sichtbar)."""
    return RUN_PROFILES.get(profile, RUN_PROFILES[""])


def spawn_claude(prompt: str, resume_id: str, claude_cmd: str, timeout: int, model: str = "",
                 journal: RunJournal | None = None, on_beat=None,
                 extra_env: dict[str, str] | None = None) -> dict:
    """Startet die headless Instanz und gibt IMMER ein Ergebnis-Dict zurück:
    {ok, reply, session_id, denials, raw_error}. Wirft nie.
    Mit journal fließt claude-stdout direkt auf Platte (Popen statt Pipe): stirbt der
    Server-Prozess, schreibt der verwaiste claude zu Ende und recover_journals() erntet.

    `timeout` ist seit 2026-07-27 die NOTBREMSE (Gesamtlaufzeit), nicht mehr die
    Arbeitszeit — gekillt wird primär bei Stillstand, s. watch_run/IDLE_TIMEOUT.
    `on_beat` bekommt bei jedem Lebenszeichen den Live-Zustand (Schritte, Werkzeug,
    Session) — daran hängt die Statusanzeige im Board."""
    prompt = apply_workflow_opt_in(prompt, model)
    cmd = [claude_cmd]
    if resume_id:
        cmd += ["--resume", resume_id]
    cmd += ["-p", prompt, "--permission-mode", "auto", "--settings", AGENT_SETTINGS]
    # Der teuerste Einzelposten des Boards, gemessen 2026-08-10 (Faden 596cd041c2e1):
    # Claude Code rendert cwd, Datum und `git status` in den System-Prompt. Dieses Repo
    # schreibt bei JEDEM Run (board.md, Threads) — der Prompt-Präfix ist damit jedes Mal
    # anders und der Prompt-Cache bricht direkt hinter dem statischen Block. Turn 1 eines
    # Folge-Runs las deshalb nur ~17k und schrieb 33k–147k neu.
    # Kontrollierter Test (gleiche Session, Resume nach 20s, Git-Stand verändert):
    #   ohne Flag  →       0 gelesen /  98.350 geschrieben
    #   mit Flag   →  98.082 gelesen /      95 geschrieben
    # Das Flag verschiebt die wechselnden Abschnitte in die erste User-Nachricht — der
    # Agent sieht sie weiterhin, der Präfix wird stabil. Preis: der git-Stand ist in
    # langen Sessions veraltet, frisch braucht es ein `git status`.
    # Wirkungslos mit --system-prompt (nutzen wir nicht). Nachgehalten wird das über
    # t1_read/t1_write in usage-log.jsonl — s. _erster_turn_cache().
    cmd += ["--exclude-dynamic-system-prompt-sections"]
    cmd += ["--disallowed-tools", *disallowed_tools(model)]  # s. UNUSED_TOOLS: −22 % Prefix
    # stream-json = eine Zeile pro Ereignis (Herzschlag). --verbose ist dabei Pflicht.
    # Das Schluss-Event hat dieselben Felder wie der alte Einzel-Envelope, deshalb ändert
    # sich für alles hinter dem Parser nichts.
    cmd += ["--output-format", "stream-json", "--verbose"] if STREAM else ["--output-format", "json"]
    alias, effort = resolve_profile(model)
    if alias:  # leer = CLI-Default (Session-Modell); Alias wie "opus"/"sonnet" schont Fable-Limits
        cmd += ["--model", alias]
    if effort:  # ohne Flag landet ein headless -p faktisch auf medium — auch wenn in
        cmd += ["--effort", effort]  # ~/.claude/settings.json etwas anderes steht (gemessen 27.07.)
    run_env = default_claude_env(RUN_ENV)
    run_env.update(extra_env or {})
    if journal is None:
        try:
            proc = subprocess.run(cmd, cwd=GC_ROOT, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=run_env)
        except subprocess.TimeoutExpired:
            # Pfad ohne Journal (CLI/Tests): kein Strom auf Platte, also auch kein
            # Stillstandsbegriff und kein zu rettender Session-Handle.
            return {"ok": False, "reply": "", "session_id": "", "denials": [],
                    "raw_error": f"Safety stop after {timeout // 60} min total runtime"}
        except FileNotFoundError:
            return {"ok": False, "reply": "", "session_id": "", "denials": [],
                    "raw_error": f"Claude binary not found ({claude_cmd})"}
        return _parse_claude_stdout(proc.stdout, proc.stderr, proc.returncode)
    # "w+" statt "w", und gelesen wird aus dem OFFENEN Handle statt noch einmal über den
    # Pfad (23.07., dieser Faden). Der Pfad-Read war der letzte Rest des [Errno 2]-
    # Crashes: `set_pid` journalisiert die pid des CLAUDE-KINDS, nicht die des Runners —
    # in der Sekunde, in der das Kind exitet, ist `_pid_alive()` false und die Journal-Wache
    # (alle 60s) darf die .out.json löschen, während dieser Aufruf sie noch lesen will.
    # Die pid-Bremse aus v0.15.6 kann dieses Fenster prinzipiell nicht schließen, sie
    # verkleinert es nur. Ein offener fd zeigt weiter auf die Inode — ein Unlink zwischen
    # wait() und read() ist damit egal, statt den ganzen Run als "Runner-Crash" zu töten.
    tail = StreamTail(journal.out_path) if STREAM else None

    def _beat(state: dict) -> None:
        journal.beat(state)  # durabel, gedrosselt
        if on_beat:
            on_beat(state)   # live an den Server für die Anzeige im Board

    try:
        with open(journal.out_path, "w+") as fo, open(journal.err_path, "w+") as fe:
            # start_new_session: eigene Prozessgruppe, damit ein Abbruch auch die vom
            # Agenten gestarteten Kinder erwischt (s. _signal_group).
            proc = subprocess.Popen(cmd, cwd=GC_ROOT, stdout=fo, stderr=fe, text=True,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env=run_env)
            journal.set_pid(proc.pid)
            reason, elapsed = watch_run(proc, tail, timeout, journal.stop_path, _beat)
            if reason:
                # Abbruch: NICHT den ganzen Strom einlesen. Der ist nach einer Stunde
                # zweistellig-MB groß, und gebraucht wird daraus genau ein Feld — die
                # session_id, damit der Run fortsetzbar bleibt. Die steht im allerersten
                # Ereignis, und meistens kennt die Wache sie ohnehin schon.
                state = dict(tail.state) if tail else {}
                if not state.get("session_id"):
                    fo.seek(0)
                    _, sid, _m = _envelope(fo.read(200_000))  # Kopf reicht: init ist Ereignis 1
                    if sid:
                        state["session_id"] = sid
                journal.mark_killed(reason, elapsed, state)
                return _kill_outcome(reason, elapsed, state, timeout)
            fo.seek(0), fe.seek(0)  # das Kind hat den geteilten Offset ans Ende geschoben
            out_txt, err_txt = fo.read(), fe.read()
    except FileNotFoundError:
        return {"ok": False, "reply": "", "session_id": "", "denials": [],
                "raw_error": f"Claude binary not found ({claude_cmd})"}
    return _parse_claude_stdout(out_txt, err_txt, proc.returncode)


def _codex_argv(codex_cmd: str, resume_id: str, model: str, prompt_path: Path,
                last_path: Path, own_home: bool = False) -> list[str]:
    """argv für einen Codex-Lauf. Ausgelagert, weil es die einzige Stelle ist, die man
    ohne echten CLI-Aufruf testen kann — und die, an der ein falscher Flag am teuersten ist.

    Reihenfolge ist nicht beliebig: `resume` ist ein Unterbefehl von `exec` und will die
    Session-ID direkt hinter sich; die Optionen stehen davor, der Prompt-Platzhalter `-`
    (stdin) ganz am Ende.

    `own_home=True` = der Lauf nutzt das Board-eigene CODEX_HOME (Phase 5): dann wollen
    wir dessen generierte config.toml GELESEN haben — `--ignore-user-config` würde genau
    sie verwerfen („Do not load $CODEX_HOME/config.toml", --help 0.147.0). Ohne eigenes
    Home bleibt das Flag drin, damit die persönlichen ChatGPT-App-Server draußen bleiben."""
    cmd = [codex_cmd, "exec", "--json", "--approve-for-me"]
    if not own_home:
        cmd.append("--ignore-user-config")
    cmd += ["--skip-git-repo-check", "-C", str(GC_ROOT), "-o", str(last_path)]
    alias, effort = resolve_profile(model)
    if alias:
        cmd += ["-m", alias]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort!r}".replace("'", '"')]
    if resume_id:
        cmd += ["resume", resume_id]
    cmd += ["-"]  # Prompt kommt über stdin (prompt_path), s. Kommentar an CODEX_CMD
    return cmd


def _codex_envelope(stdout: str) -> tuple[dict, str, str]:
    """Codex-JSONL → (usage-Dict, thread_id, letzte agent_message).

    Anders als bei claude gibt es kein Schluss-Event, das alles trägt: die Zahlen stehen
    in `turn.completed`, die Session in `thread.started`, der Text in der letzten
    `agent_message`. Nicht-JSON-Zeilen kommen real vor (CLI-Fehler werden als Klartext
    geschrieben) und werden übersprungen, nicht als Fehler gewertet."""
    usage, thread_id, last_msg, failed = {}, "", "", ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # halbe Schlusszeile eines gekillten Runs
        if not isinstance(ev, dict):
            continue
        typ = ev.get("type")
        if typ == "thread.started" and ev.get("thread_id"):
            thread_id = str(ev["thread_id"])
        elif typ == "turn.completed":
            usage = ev.get("usage") or {}
        elif typ in ("turn.failed", "error"):
            failed = str((ev.get("error") or {}).get("message") or ev.get("message") or typ)
        elif typ == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_msg = str(item["text"])
    if failed and not last_msg:
        last_msg = ""
    return {"usage": usage, "failed": failed}, thread_id, last_msg


def _codex_rollout_usage(thread_id: str, sessions_root: Path | None = None) -> dict:
    """Per-Request-Usage des LETZTEN Codex-CLI-Runs aus seinem lokalen Rollout.

    Der öffentliche ``codex exec --json``-Strom liefert in ``turn.completed`` nur die
    über alle Modellaufrufe kumulierte Turn-Summe. Der Rollout trennt dagegen bei jedem
    ``token_count`` zwischen ``total_token_usage`` und ``last_token_usage``. Ein neues
    ``task_started`` markiert jeden frischen ``exec``-/``resume``-Prozess innerhalb
    derselben Session-Datei. Damit sind erster und letzter Snapshot danach genau die
    beiden Messpunkte, die wir brauchen: Cross-Run-Cache und residenter Kontext.

    Best effort: Das ist CLI-interne Observability. Fehlt oder driftet sie, bleibt die
    Anzeige unbekannt; niemals auf die kumulierte Turn-Summe zurückfallen.
    """
    if not thread_id:
        return {}
    root = sessions_root or (Path.home() / ".codex" / "sessions")
    try:
        hits = list(root.rglob(f"rollout-*{thread_id}.jsonl"))
        if not hits:
            return {}
        path = max(hits, key=lambda p: p.stat().st_mtime)
        first: dict = {}
        last: dict = {}
        task_count = 0
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "event_msg":
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") == "task_started":
                    task_count += 1
                    first, last = {}, {}  # nur der aktuelle Prozess/Resume-Lauf
                    continue
                if payload.get("type") != "token_count" or task_count == 0:
                    continue
                usage = ((payload.get("info") or {}).get("last_token_usage") or {})
                if not isinstance(usage, dict) or not usage.get("input_tokens"):
                    continue
                if not first:
                    first = usage
                last = usage
        return {"first_request": first, "last_request": last,
                "task_count": task_count, "source": "codex-rollout"} if last else {}
    except OSError:
        return {}


def _codex_usage_summary(usage: dict, model: str, snapshots: dict | None = None) -> dict:
    """Codex-Tokens in das Schema von usage-log.jsonl übersetzen.

    Bewusste Lücke: Codex liefert KEINE Kosten. `cost_usd` bleibt None, statt eine Zahl
    aus einer Preistabelle zu erfinden, die niemand abrechnet (Entscheidungsblatt Frage 7
    = A). Die Kosten-Kachel zeigt damit weiterhin ausschließlich echte Claude-Kosten."""
    def n(k: str) -> int:
        try:
            return int(usage.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    read, inp, out = n("cached_input_tokens"), n("input_tokens"), n("output_tokens")
    # Codex zählt `input_tokens` inklusive des gecachten Anteils (beobachtet 11.08.:
    # input 26.654 / cached 11.008 im selben Turn) — anders als claude, wo beides
    # disjunkt ist. Für die Prozentzahl ist input also schon die Gesamtsumme.
    summary = {"input_tokens": inp, "cache_read": read, "cache_creation": n("cache_write_input_tokens"),
            "output_tokens": out,
            "cache_hit_pct": round(100 * read / inp) if inp else None,
            "cost_usd": None, "duration_ms": None, "num_turns": None,
            # Welches Modell wirklich lief, sagt der Strom nicht — bei leerem Alias
            # entscheidet die Codex-Konfiguration. Also „default" statt einer Behauptung.
            "models": [f"codex:{resolve_profile(model)[0] or 'default'}"],
            "reasoning_output_tokens": n("reasoning_output_tokens")}
    snapshots = snapshots or {}

    def snap_n(which: str, key: str) -> int:
        try:
            return int((snapshots.get(which) or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0

    first_in = snap_n("first_request", "input_tokens")
    first_read = snap_n("first_request", "cached_input_tokens")
    last_in = snap_n("last_request", "input_tokens")
    last_read = snap_n("last_request", "cached_input_tokens")
    if last_in:
        summary.update(last_request_input_tokens=last_in,
                       last_request_cache_read=last_read,
                       context_source=snapshots.get("source", "codex-rollout"))
    if first_in:
        summary.update(cross_run_input_tokens=first_in,
                       cross_run_cache_read=first_read,
                       cross_run_cache_hit_pct=round(100 * first_read / first_in),
                       cross_run_cache_scope="first-request-of-cli-run")
    return summary


def _parse_codex_stdout(stdout: str, stderr: str, returncode: int | None,
                        last_path: Path | None = None, model: str = "",
                        sessions_root: Path | None = None) -> dict:
    """Codex-Ausgabe → dasselbe Outcome-Dict, das der Claude-Pfad liefert.

    Die Schlussnachricht kommt bevorzugt aus der `-o`-Datei: die schreibt Codex selbst und
    genau einmal, während die letzte `agent_message` im Strom auch mal eine Zwischenansage
    sein kann."""
    env, thread_id, last_msg = _codex_envelope(stdout)
    reply = ""
    if last_path is not None:
        try:
            reply = last_path.read_text().strip()
        except OSError:
            reply = ""
    reply = reply or last_msg.strip()
    snapshots = _codex_rollout_usage(thread_id, sessions_root)
    usage_summary = _codex_usage_summary(env.get("usage") or {}, model, snapshots)
    # Der ehrliche Snapshot kommt aus ``last_token_usage``. ``turn.completed.input_tokens``
    # ist Run-Verbrauch und kann bei vielen Werkzeugschritten Millionen erreichen.
    context_tokens = usage_summary.get("last_request_input_tokens", 0)
    if env.get("failed") or (returncode not in (0, None)) or not reply:
        grund = env.get("failed") or (f"exit {returncode}" if returncode else "no final message")
        return {"ok": False, "runner": "codex", "reply": reply, "session_id": thread_id, "denials": [],
                "context_tokens": context_tokens,
                "usage_summary": usage_summary,
                "raw_error": f"no result from Codex ({grund}): {_lesbarer_rest(stderr or stdout)}"}
    return {"ok": True, "runner": "codex", "reply": reply, "session_id": thread_id, "denials": [],
            "context_tokens": context_tokens,
            "usage_summary": usage_summary, "raw_error": ""}


class CodexStreamTail(StreamTail):
    """StreamTail für das Codex-Ereignisschema — gleiche Zustandsfelder, anderer Strom.

    Der Werkzeugname steckt bei Codex nicht in einem eigenen Feld; der Item-TYP ist der
    Diskriminator (`command_execution`, `file_change`, `mcp_tool_call`, …). Bei
    `command_execution` nehmen wir zusätzlich den Anfang des Befehls, sonst stünde in der
    Live-Anzeige bei jedem Shell-Aufruf dasselbe Wort."""

    def _absorb(self, line: str) -> None:  # noqa: C901 — flach, nur viele Ereignistypen
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(ev, dict):
            return
        typ = ev.get("type")
        if typ == "thread.started" and ev.get("thread_id"):
            self.state["session_id"] = str(ev["thread_id"])
        item = ev.get("item") or {}
        itype = item.get("type") if isinstance(item, dict) else None
        if itype in CODEX_TOOL_ITEMS:
            name = itype
            if itype == "command_execution":
                name = str(item.get("command") or itype).strip().split("\n")[0][:40]
            elif itype == "mcp_tool_call":
                name = f"{item.get('server', '?')}/{item.get('tool', '?')}"
            if typ == "item.started":
                self.state["steps"] += 1
                self.state["last_tool"] = name
                self.state["busy_tool"] = name
                # Codex-Namen (Kommando-Präfix, mcp server/tool) liegen nie in
                # AGENT_TOOLS → für Codex gilt immer die normale Werkzeug-Frist.
                self._open[str(item.get("id", ""))] = name
            elif typ == "item.completed":
                self._open.pop(str(item.get("id", "")), None)
        self.state["busy"] = len(self._open)
        if not self._open:
            self.state["busy_tool"] = ""
        if self.on_event is not None:
            self.on_event(ev)


# Wo Codex projektlokal nach Skills sucht. `.agents/skills` ist der Pfad, den er im Test
# vom 12.08. TATSÄCHLICH gezogen hat (er nannte ihn selbst in der Antwort); `.codex/skills`
# ist der dokumentierte. Beide zeigen auf dieselbe Quelle, damit die Wahl egal ist.
CODEX_SKILL_LINKS = (Path(".codex") / "skills", Path(".agents") / "skills")


def ensure_codex_skills(root: Path | None = None) -> dict[str, str]:
    """Codex' Skill-Verzeichnisse als Symlinks auf `.claude/skills` halten (Phase 4 des
    Codex-Plans). Idempotent, läuft vor jedem Codex-Lauf.

    Die Links selbst sind seit 12.08. in Git eingecheckt (gitignore-Ausnahme `!.codex/skills`
    / `!.agents/skills`; der Rest von `.codex/` bleibt ignoriert). Frische Checkouts haben die
    Links also schon; diese Funktion ist der Wächter dagegen, dass jemand sie löscht oder
    durch echte Verzeichnisse ersetzt.

    In `.codex/` liegt seit 12.08. bewusst KEINE `config.toml` mehr: sie wirkte in jedem
    Codex-Lauf im Repo mit und hätte die kuratierte MCP-Auswahl der generierten Board-Config
    unterlaufen (s. ARCHITEKTUR.md; Nacht-Wache in tools/context-health-check.py).

    Warum ein Verzeichnis-Link statt Kopien: `.agents/skills` WAR eine Kopie und war am
    12.08. nachweislich veraltet (2 Skills fehlten, 8 Dateien drifteten, Stand 06.08.) —
    Codex-Runs liefen also auf alten Skills. Ein Link kann nicht driften.

    Fasst nur an, was leer ist oder schon unser Link ist; ein echtes Verzeichnis bleibt
    unangetastet. Gibt {pfad: status} zurück (für Tests und Log); wirft nie — ein fehlender
    Link ist kein Grund, einen Run zu killen."""
    root = root or GC_ROOT
    target = root / ".claude" / "skills"
    ergebnis: dict[str, str] = {}
    for rel in CODEX_SKILL_LINKS:
        link = root / rel
        try:
            if not target.is_dir():
                ergebnis[str(rel)] = "kein-quellverzeichnis"
                continue
            if link.is_symlink():
                ergebnis[str(rel)] = "ok" if link.resolve() == target.resolve() else "fremder-link"
                continue
            if link.exists():
                ergebnis[str(rel)] = "belegt"
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(Path("..") / ".claude" / "skills")
            ergebnis[str(rel)] = "angelegt"
        except OSError:
            ergebnis[str(rel)] = "fehler"
    return ergebnis


# v0 deliberately forwards no MCP servers or their credential variables. An
# explicit, reviewed integration boundary can be added later without silently
# inheriting private-instance tooling into every installation.
CODEX_MCP_SERVERS: tuple[str, ...] = ()


def _toml_str(v: str) -> str:
    # JSON-Escaping ist für unsere Werte (Pfade, Tokens, Servernamen) gültiges TOML.
    return json.dumps(v)


def generate_codex_config(mcp: dict, root: Path) -> tuple[str, dict[str, str]]:
    """`.mcp.json` → (config.toml-Text, Secret-Env) für das Board-CODEX_HOME (Phase 5).

    Secrets-Weg (am Binary 0.147.0-alpha.6.5 verifiziert, 12.08.): Codex cleart beim
    MCP-stdio-Spawn das Env auf ein 12-Var-Core-Set und reicht NUR durch, was pro Server
    als `env_vars = ["NAME"]`-Allowlist deklariert ist — der Wert kommt dann live aus dem
    Env des Codex-Prozesses. Deshalb stehen in der generierten Datei nur NAMEN; die Werte
    liefert der Runner übers Spawn-Env (zweiter Rückgabewert). `${VAR}` in config.toml
    expandiert Codex nicht, und `shell_environment_policy` wirkt auf MCP-Spawns gar nicht
    (beides empirisch geprüft). `inherit = "core"` hält die injizierten Tokens zusätzlich
    aus den Shell-Kommandos des Agents heraus.

    Nur die explizite Auswahl CODEX_MCP_SERVERS wandert mit — und nur stdio-
    Server. Kollision (gleicher Var-Name, verschiedene Werte) → ValueError, der Aufrufer
    fällt dann auf den MCP-losen Modus zurück statt falsche Secrets zu liefern."""
    lines = [
        "# GENERIERT von gc_runner.generate_codex_config aus .mcp.json — nie von Hand pflegen.",
        "",
        "[shell_environment_policy]",
        'inherit = "core"',
        "",
        f"[projects.{_toml_str(str(root))}]",
        'trust_level = "trusted"',
    ]
    secret_env: dict[str, str] = {}
    servers = mcp.get("mcpServers", {})
    for name in CODEX_MCP_SERVERS:
        srv = servers.get(name)
        if not srv or srv.get("url") or not srv.get("command"):
            continue  # fehlt oder HTTP — beides (noch) nicht Teil der Kernauswahl
        lines += ["", f"[mcp_servers.{_toml_str(name)}]",
                  f"command = {_toml_str(srv['command'])}",
                  "args = [" + ", ".join(_toml_str(a) for a in srv.get("args") or []) + "]"]
        env = srv.get("env") or {}
        for k, v in env.items():
            if secret_env.get(k, v) != v:
                raise ValueError(f"Env-Kollision zwischen MCP-Servern: {k}")
            secret_env[k] = v
        if env:
            lines.append("env_vars = [" + ", ".join(_toml_str(k) for k in env) + "]")
    return "\n".join(lines) + "\n", secret_env


CODEX_SHARED_STATE = ("auth.json", "sessions")


def _link_shared_codex_state(home: Path, user_home: Path | None = None) -> dict[str, str]:
    """Was das Board-Home mit `~/.codex` TEILT, statt es zu duplizieren: die Anmeldung
    und die Sitzungshistorie. Isoliert wird nur die `config.toml` — sie ist der einzige
    Teil, der uns fremde MCP-Server einschleppen würde.

    Das spart den zweiten `codex login` (am 12.08. verifiziert: `login status` im
    Board-Home meldet „Logged in using ChatGPT") und hält Codex-Fäden resumebar, weil die
    Sessions weiter unter dem gewohnten Pfad liegen — sonst hätte der Umstieg aufs
    Board-Home jeden laufenden Codex-Faden gekappt.

    Legt NUR an, was fehlt. Eine echte Datei bleibt unangetastet: schreibt Codex beim
    Token-Refresh den Symlink weg, gilt die neue Datei — ein Zurücklinken würde frische
    Tokens gegen ältere tauschen."""
    ziel_basis = user_home or (Path.home() / ".codex")
    ergebnis: dict[str, str] = {}
    for name in CODEX_SHARED_STATE:
        link, ziel = home / name, ziel_basis / name
        try:
            if not ziel.exists():
                ergebnis[name] = "kein-ziel"
            elif link.is_symlink():
                ergebnis[name] = "ok" if link.resolve() == ziel.resolve() else "fremder-link"
            elif link.exists():
                ergebnis[name] = "belegt"
            else:
                link.symlink_to(ziel)
                ergebnis[name] = "angelegt"
        except OSError:
            ergebnis[name] = "fehler"
    return ergebnis


def prepare_codex_home(root: Path | None = None) -> tuple[Path | None, dict[str, str]]:
    """Board-owned CODEX_HOME under the workspace runtime directory (gitignored)
    bzw. aktuell halten. Rückgabe (home, secret_env); (None, {}) wenn kein MCP-Setup
    möglich ist (keine/kaputte .mcp.json, Env-Kollision, FS-Fehler) — der Lauf fällt
    dann still auf das bisherige Verhalten zurück (Default-Home + --ignore-user-config,
    ohne MCP). Wirft nie: MCP ist Komfort, kein Grund, einen Run zu killen."""
    root = root or GC_ROOT
    data = _p.DATA if root == GC_ROOT else root / ".superboard"
    home = data / "codex-home"
    try:
        mcp = json.loads((root / ".mcp.json").read_text())
        toml_text, secret_env = generate_codex_config(mcp, root)
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        cfg = home / "config.toml"
        if not cfg.exists() or cfg.read_text() != toml_text:
            cfg.write_text(toml_text)
        _link_shared_codex_state(home)
        return home, secret_env
    except (OSError, ValueError, json.JSONDecodeError):
        return None, {}


def codex_home_ready(home: Path | None) -> bool:
    """MCP-Modus nur mit gültiger Anmeldung im Board-Home. Die kommt über den Symlink auf
    `~/.codex/auth.json` (s. `_link_shared_codex_state`) — der im Plan vorgesehene zweite
    `codex login` hat sich damit erledigt. Fehlt die Anmeldung ganz, läuft Codex weiter wie
    bisher, nur ohne MCP: kein Grund, einen Lauf zu verweigern.

    `is_file()` folgt dem Symlink — genau gewollt: ein toter Link zählt nicht als bereit."""
    return home is not None and (home / "auth.json").is_file()


def spawn_codex(prompt: str, resume_id: str, codex_cmd: str, timeout: int, model: str = "",
                journal: RunJournal | None = None, on_beat=None,
                extra_env: dict[str, str] | None = None) -> dict:
    """Gegenstück zu spawn_claude für die Codex CLI — gleiche Signatur, gleiches
    Rückgabe-Dict, wirft nie. Prozesskontrolle (eigene Prozessgruppe, Stillstands-Wache,
    Stopp-Knopf) ist bewusst dieselbe: das ist der Teil, der teuer erarbeitet wurde."""
    ensure_codex_skills()
    home, secret_env = prepare_codex_home()
    own_home = codex_home_ready(home)
    # Secrets landen NUR im Spawn-Env dieses einen Prozesses (env_vars-Allowlist reicht
    # sie an die deklarierten MCP-Server weiter) — nie in einer Datei, nie in RUN_ENV.
    # Codex has its own account/config boundary.  Do not inject the Claude private-config pin
    # into it merely because both runners share process-control code.
    codex_base_env = without_claude_account_env(BASE_ENV)
    run_env = ({**codex_base_env, **secret_env, "CODEX_HOME": str(home)}
               if own_home else codex_base_env)
    run_env.update(extra_env or {})
    base = journal.out_path if journal is not None else Path(tempfile.mkdtemp(prefix="gc-codex-")) / "run.out"
    base.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = base.with_suffix(".prompt")
    last_path = base.with_suffix(".last")
    try:
        prompt_path.write_text(prompt)
    except OSError as e:
        return {"ok": False, "reply": "", "session_id": "", "denials": [],
                "raw_error": f"Cannot write prompt file: {e}"}
    cmd = _codex_argv(codex_cmd, resume_id, model, prompt_path, last_path, own_home)
    if journal is None:
        try:
            with open(prompt_path) as fin:
                proc = subprocess.run(cmd, cwd=GC_ROOT, capture_output=True, text=True,
                                      timeout=timeout, stdin=fin, env=run_env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reply": "", "session_id": "", "denials": [],
                    "raw_error": f"Safety stop after {timeout // 60} min total runtime"}
        except FileNotFoundError:
            return {"ok": False, "reply": "", "session_id": "", "denials": [],
                    "raw_error": f"Codex binary not found ({codex_cmd})"}
        return _parse_codex_stdout(proc.stdout, proc.stderr, proc.returncode, last_path, model)
    tail = CodexStreamTail(journal.out_path)

    def _beat(state: dict) -> None:
        journal.beat(state)
        if on_beat:
            on_beat(state)

    try:
        with open(journal.out_path, "w+") as fo, open(journal.err_path, "w+") as fe, \
                open(prompt_path) as fin:
            proc = subprocess.Popen(cmd, cwd=GC_ROOT, stdout=fo, stderr=fe, text=True,
                                    stdin=fin, start_new_session=True, env=run_env)
            journal.set_pid(proc.pid)
            reason, elapsed = watch_run(proc, tail, timeout, journal.stop_path, _beat)
            if reason:
                state = dict(tail.state)
                journal.mark_killed(reason, elapsed, state)
                return {**_kill_outcome(reason, elapsed, state, timeout), "runner": "codex"}
            fo.seek(0), fe.seek(0)
            out_txt, err_txt = fo.read(), fe.read()
    except FileNotFoundError:
        return {"ok": False, "runner": "codex", "reply": "", "session_id": "", "denials": [],
                "raw_error": f"Codex binary not found ({codex_cmd})"}
    return _parse_codex_stdout(out_txt, err_txt, proc.returncode, last_path, model)


def parse_by_runner(model: str, stdout: str, stderr: str, returncode: int | None,
                    out_path: Path | None = None) -> dict:
    """Journal-Recovery: welcher Parser gilt für diesen liegengebliebenen Strom? Die
    Antwort steht im Journal — dort ist das Lauf-Profil mitgeschrieben."""
    if runner_of(model) == "codex":
        last = out_path.with_suffix(".last") if out_path is not None else None
        return _parse_codex_stdout(stdout, stderr, returncode, last, model)
    return _parse_claude_stdout(stdout, stderr, returncode)


def spawn_agent(prompt: str, resume_id: str, claude_cmd: str, timeout: int, model: str = "",
                journal: RunJournal | None = None, on_beat=None,
                extra_env: dict[str, str] | None = None) -> dict:
    """Einzige Weiche zwischen den Runnern. Welcher läuft, entscheidet allein das
    gewählte Profil — es gibt bewusst kein zweites Auswahlfeld neben dem Modell.
    Account routing is intentionally outside the profile picker."""
    runner = runner_of(model)
    if runner == "codex":
        return spawn_codex(prompt, resume_id, CODEX_CMD, timeout, model, journal, on_beat,
                           extra_env)
    return spawn_claude(prompt, resume_id, claude_cmd, timeout, model, journal, on_beat,
                        extra_env)


def _write_sidecar(gc_id: str, title: str, full_text: str, sidecar_dir: Path) -> Path:
    return _sc.write_sidecar(gc_id, title, full_text, sidecar_dir, kind="reply")


def _inline_reply(gc_id: str, title: str, reply: str, sidecar_dir: Path) -> str:
    """@gc-re: ist eine Markdown-EINZEILE. Mehrzeiliges/Langes → Sidecar-Datei,
    inline nur erste Zeile + Verweis. (Logik lebt in sidecar.py — seit der
    board.md-Diät gilt dieselbe Regel auch für die @gc:-Turns des owners im Server.)"""
    return _sc.inline_turn(gc_id, title, reply, sidecar_dir, kind="reply")


def _with_denial_note(text: str, n: int) -> str:
    """Denial-Warnung einfügen, OHNE den Sidecar-Verweis vom Zeilenende zu schubsen.

    `markers.REF_RE` ist auf `\\s*$` verankert — der Verweis MUSS das Letzte auf der
    Zeile sein. Angehängt hinter den Verweis kostet die Warnung deshalb den ganzen
    Volltext: `sidecar.expand()` findet nichts mehr, die UI zeigt nur den Kurzsatz,
    und `server.item_sheet()` sieht das Entscheidungsblatt nicht, weil dessen Pfad
    erst im Sidecar steht (Faden 2ddd73779387, 14.08.: Blatt wurde nicht angezeigt).
    """
    note = f"⚠️ ({n} action(s) blocked by the permission classifier)"
    m = _sc.REF_RE.search(text)
    if not m:
        return f"{text} {note}"
    return f"{text[:m.start()].rstrip()} {note} {text[m.start():]}"


def _post_append(base_url: str, gc_id: str, text: str, session: str, gc_last: str = "") -> None:
    """Antwort zurück ins Board — mit Retries, weil das der einzige Weg ist,
    auf dem der Run sichtbar wird. Wirft nach 3 Fehlversuchen."""
    payload = {"kind": "reply", "text": text, "addr": {"id": gc_id}}
    if session:
        payload["session"] = session
    if gc_last:
        payload["gc_last"] = gc_last  # Run-Meta (Kontextgröße + Zeitpunkt) fürs Overlay
    req = urllib.request.Request(f"{base_url}/api/gc-append", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                json.load(r)
            return
        except Exception as e:  # noqa: BLE001 — bewusst breit: jeder Fehler → retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gc-append fehlgeschlagen nach 3 Versuchen: {last}")


def fail_stamp(marker: str = "❌") -> str:
    """Der @gc-last-Fehlstempel — EIN Ort, weil ihn drei Pfade brauchen: der normal
    fehlgeschlagene Run (_outcome), der Runner-Crash (server.launch_gc_run) und der
    verwaiste Run aus der Journal-Recovery. 23.07.: ein Crash postete zwar ein ❌
    in den Faden, stempelte aber nichts — und damit blieb die Karte in der Matrix
    unauffällig, genau die Lücke, die der ✕-Stempel schließen sollte. Form wie der
    Erfolgsstempel (erstes Feld · Zeit), bewusst ohne Grund: der steht im Faden.

    marker="⏹" für den selbst gedrückten Stopp: das ist kein Fehler und soll die Karte
    nicht wie einen kaputten Run aussehen lassen."""
    return f"{marker} · {time.strftime('%Y-%m-%d %H:%M')}"


def _outcome(out: dict, gc_id: str, title: str, sidecar_dir: Path) -> tuple[str, str, str]:
    """Outcome-Dict → (@gc-re-Text, @gc-session-Zeile, @gc-last-Meta). Gemeinsam für
    Live-Run und Journal-Recovery, damit eine geerntete Antwort exakt so aussieht wie
    eine live gepostete. @gc-last ("~85k · 2026-07-16 14:32") nur bei erfolgreichem Run
    mit ermittelbarer Kontextgröße — Fehlläufe stempeln nichts."""
    if out["ok"]:
        text = _inline_reply(gc_id, title, out["reply"] or "(empty reply)", sidecar_dir)
        if out["denials"]:
            text = _with_denial_note(text, len(out["denials"]))
    elif out.get("killed"):
        # Abbruch-Meldungen bringen ihr eigenes Symbol und ihren eigenen Wortlaut mit
        # (⏹ gestoppt / ❌ Stillstand / ❌ Notbremse) — kein zweites „fehlgeschlagen"
        # davorsetzen, sonst liest sich ein selbst gedrückter Stopp wie ein Absturz.
        text = out["raw_error"]
    else:
        detail = out["raw_error"] or "unknown error"
        text = f"❌ Agent run failed: {detail}"
        if out["reply"]:
            text += " — " + _inline_reply(gc_id, title, out["reply"], sidecar_dir)
    sid = out.get("session_id") or ""
    if not SESSION_ID_RE.match(sid):  # nur UUID-artiges persistieren — Müll würde --resume brechen
        sid = ""
    ctx = out.get("context_tokens") or 0
    if out["ok"]:
        # Auch bei ctx==0 stempeln: vorher verschwand bei einem nicht lesbaren usage-Block
        # der GANZE Stempel (Kontext UND Kosten, s. unten) und am Item blieb still der alte
        # stehen — ein erfolgreicher Run sah dann aus wie „nie gelaufen" (2026-08-25).
        # „~0k" ist ehrlich und formatstabil.
        gc_last = f"~{round(ctx / 1000)}k · {time.strftime('%Y-%m-%d %H:%M')}"
    elif not out["ok"]:
        # Fehlläufe stempeln jetzt auch (2026-07-22, Blatt Q3=A). Bewusst OHNE Grund:
        # „stempel braucht keinen grund, reicht ja wenn ich sehe, dann kann ich reinschauen
        # wieso". Vorher stempelte ein toter Run gar nichts — der Spend-Limit-Abbruch am
        # 21.07. hinterließ am Item keine Spur, sichtbar nur beim zufälligen Öffnen des
        # Fadens. Gleiche Form wie der Erfolgsstempel (erstes Feld · Zeit · optional Kosten),
        # damit Frontend-Ersetzung und board_kpis („Runs heute") unverändert greifen.
        gc_last = fail_stamp("⏹" if out.get("killed") == "stop" else "❌")
    else:
        gc_last = ""
    # Kosten-Transparenz (2026-07-22, Blatt e67ba06428b7 Q7=C): statt eines
    # --max-budget-usd-Deckels (Abbruch mitten im Tool-Call = halbfertige Änderungen)
    # erst mal SEHEN, was ein Run gekostet hat — "ach 34 Euro, lass uns das nochmal
    # betrachten". Ans ENDE des Stempels, damit die Frontend-Ersetzung des ERSTEN
    # " · " ("letzter Run") und die "kompaktiert"-Prefix-Prüfung unberührt bleiben.
    cost = (out.get("usage_summary") or {}).get("cost_usd")
    if gc_last and isinstance(cost, (int, float)) and cost > 0:
        gc_last += f" · ${cost:.2f}"
    # Runner-Marker nur bei den Nicht-Default-Runnern anhängen — s. session_runner().
    # Damit bleiben alle bestehenden Claude-Session-Zeilen zeichengleich.
    runner = out.get("runner")
    marke = " · codex" if runner == "codex" else ""
    return text, (f"{sid} · board-{_slug(title)}{marke}" if sid else ""), gc_last


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _open_gc_ids(base_url: str) -> set[str]:
    """Items, die gerade auf GC warten (⏳). Recovery postet NUR dorthin — sonst würde
    ein Journal, dessen Append in Wahrheit durchging, die Antwort ein zweites Mal anhängen."""
    with urllib.request.urlopen(f"{base_url}/api/gc-pending", timeout=10) as r:
        return {p["addr"].get("id") for p in json.load(r)["pending"]}


def recover_journals(base_url: str = DEFAULT_URL, journal_dir: Path | None = None,
                     sidecar_dir: Path = SIDECAR_DIR, skip_ids: set[str] | None = None) -> list[str]:
    """Erntet liegengebliebene Runs (Server-Neustart/Crash mitten im Run — real passiert
    am 2026-07-14: die Antwort war weg, das Item hing für immer auf ⏳GC).

    Pro Journal:
      status=ready          → Antworttext liegt fertig auf Platte → posten.
      running, Prozess lebt → verwaister claude schreibt noch → in Ruhe lassen, nächster Durchlauf.
      running, Prozess tot  → stdout aus dem Journal parsen (der Prozess hat evtl. zu Ende
                              geschrieben, nur der Poster starb) → posten; ist da nichts
                              Brauchbares und das Journal ist älter als RECOVER_GRACE,
                              wird ein sichtbares ❌ gepostet, statt das Item hängen zu lassen.
    Läuft idempotent: gepostet wird nur, wenn das Item noch auf GC wartet; danach Journal weg.

    skip_ids: gc_ids, die JETZT im selben Serverprozess laufen (RUNNING-Registry). Ohne
    diesen Filter kann die periodische Journal-Wache (alle RECOVER_EVERY=60s) einen Run
    "ernten", dessen Subprozess gerade eben exitet ist, aber dessen live run_item()-Aufruf
    aus spawn_claude() heraus noch dabei ist, out_path zu lesen — die Wache läuft das Journal
    parallel ab und löscht die Datei, der live Read trifft ins Leere: FileNotFoundError,
    sichtbar als "❌ Runner-Crash: [Errno 2] ..." im Board (real passiert 2026-07-15,
    run-0cd76e9c2405-...). Ein gc_id in RUNNING gehört exklusiv seinem live Aufruf."""
    journal_dir = journal_dir or JOURNAL_DIR  # late-bound, s. RunJournal.__init__
    metas = sorted(journal_dir.glob("run-*.meta.json")) if journal_dir.exists() else []
    if not metas:
        return []
    try:
        open_ids = _open_gc_ids(base_url)
    except Exception as e:  # noqa: BLE001 — Board (noch) nicht erreichbar: nächster Durchlauf
        return [f"recover: board unavailable ({e})"]

    notes: list[str] = []
    for meta_path in metas:
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            meta_path.unlink(missing_ok=True)  # halbes/kaputtes Meta ist nichts wert
            continue
        gc_id = meta.get("gc_id", "")
        if skip_ids and gc_id in skip_ids:  # eigener live Run — Finger weg, der räumt selbst auf
            continue
        title = meta.get("title", "")
        base = meta_path.with_suffix("")  # …/run-<id>-<ts>-<rnd>.meta → .out.json/.err.txt
        out_path, err_path = base.with_suffix(".out.json"), base.with_suffix(".err.txt")
        # .stop MUSS mit weg, sonst bleibt nach jedem geernteten Stopp eine Marke liegen
        # (discard() räumt sie, die Recovery tat es bis 2026-07-27 nicht).
        drop = [meta_path, out_path, err_path, base.with_suffix(".stop")]

        if gc_id not in open_ids:  # Antwort ist schon im Faden (oder Item geschlossen)
            # ABER: nur aufräumen, wenn hier wirklich niemand mehr arbeitet. Dieser Zweig
            # löschte früher blind — und riss damit einem LAUFENDEN Run die .out.json unter
            # den Füßen weg (23.07., Faden 6b525d987c57: "❌ Runner-Crash: [Errno 2] …
            # run-6b525d987c57-…-45fc.out.json"). Auslöser: zwei Runs nacheinander auf
            # DEMSELBEN Item — der owner schickte einen neuen Turn, während der vorige Run
            # noch lief. Sobald Run A antwortet, ist das Item nicht mehr @gc:-pending, also
            # fällt der noch laufende Run B in genau diesen Zweig. `skip_ids` schützt ihn
            # nicht zuverlässig: die Registry ist pro gc_id, nicht pro Run (Lücke zwischen
            # RUNNING.pop von A und RUNNING[gc_id] von B), und ein manueller CLI-Run
            # (`python3 gc_runner.py --id …`) steht überhaupt nie darin.
            # Derselbe Fehler war am 15.07. schon einmal da (s. Docstring) und wurde nur
            # über skip_ids adressiert — der Lebendigkeits-Check hier ist die Bremse, die
            # unabhängig von jeder Registry hält.
            if _pid_alive(meta.get("pid")):
                notes.append(f"recover: {gc_id} is no longer pending, but the run is still alive — keeping journal")
                continue
            if time.time() - float(meta.get("started", 0)) < RECOVER_GRACE:
                continue  # pid evtl. noch nicht gesetzt (Startfenster) — nicht vorschnell löschen
            for p in drop:
                p.unlink(missing_ok=True)
            continue

        if meta.get("status") == "ready":
            text, session, gc_last = meta.get("reply_text", ""), meta.get("session", ""), meta.get("gc_last", "")
        elif meta.get("status") == "killed":
            # Ein gekillter Run ist NICHT verwaist — sein run_item() räumt gerade auf
            # (Kill-Log schreiben, Receipt, Antwort posten). Die pid ist dabei schon tot,
            # der Run fiele also in den Parse-Zweig unten: die Wache würde ein generisches
            # ❌ posten und das Journal löschen, WÄHREND der lebende Aufruf noch daran
            # arbeitet — doppelter Faden-Turn plus der alte [Errno 2]-Fehler. Für
            # Server-Runs schützt skip_ids, für CLI-Runs (`python3 gc_runner.py --id …`)
            # nichts. Deshalb: Gnadenfrist abwarten und erst dann als wirklich verwaist
            # behandeln. (Review-Fund F2, 2026-07-27)
            if time.time() - float(meta.get("started", 0)) < RECOVER_GRACE:
                continue
            beat = meta.get("beat", {}) or {}
            text, session, gc_last = _outcome(
                {**_kill_outcome(meta.get("kill_reason", "cap"), float(meta.get("elapsed", 0)),
                                 beat, int(meta.get("timeout", DEFAULT_TIMEOUT))),
                 "runner": runner_of(meta.get("model", ""))},
                gc_id, title, sidecar_dir)
        elif _pid_alive(meta.get("pid")):
            notes.append(f"recover: {gc_id} is still running (pid {meta['pid']}) — recover later")
            continue
        else:
            stdout = out_path.read_text() if out_path.exists() else ""
            stderr = err_path.read_text() if err_path.exists() else ""
            if not stdout.strip():
                if time.time() - float(meta.get("started", 0)) < RECOVER_GRACE:
                    continue  # gerade erst gestartet, pid noch nicht gesetzt → nicht vorschnell töten
                text, session, gc_last = ("❌ Agent run aborted (server restart or crash) — "
                                          "no reply in the journal. Please restart it."), "", fail_stamp()
            else:
                text, session, gc_last = _outcome(
                    parse_by_runner(meta.get("model", ""), stdout, stderr, None, out_path),
                    gc_id, title, sidecar_dir)
        try:
            _post_append(base_url, gc_id, text, session, gc_last)
        except RuntimeError as e:
            notes.append(f"recover: {gc_id} — append failed ({e}); keeping journal")
            continue
        for p in drop:
            p.unlink(missing_ok=True)
        notes.append(f"recover: {gc_id} ('{title}') appended")
    return notes


def run_item(pending: dict, base_url: str = DEFAULT_URL, claude_cmd: str = PRIVATE_CMD,
             timeout: int = DEFAULT_TIMEOUT, sidecar_dir: Path = SIDECAR_DIR,
             model: str = "", journal_dir: Path | None = None, on_beat=None) -> dict:
    """Kompletter Run für EIN pending-Item (Shape von /api/gc-pending, plus
    body/thread). Gibt das Outcome-Dict zurück; schreibt das Ergebnis via
    /api/gc-append zurück ins Board. Jeder Schritt hinterlässt eine Journal-Spur
    (RunJournal) — ein Server-Neustart kann die Antwort nicht mehr verschlucken.

    on_beat(state) wird bei jedem Lebenszeichen gerufen (Server hängt daran seine
    Live-Anzeige); zusätzlich bekommt der Aufrufer über `journal` den Pfad, unter dem
    ein Stopp-Wunsch abgelegt werden kann."""
    gc_id = pending["addr"]["id"]
    title = pending.get("title", "")
    resume_id = "" if session_cut(pending.get("thread", [])) else session_uuid(pending.get("session", ""))
    if resume_id and session_runner(pending.get("session", "")) != runner_of(model):
        # Runner gewechselt (Codex ↔ Claude): der gespeicherte Handle gehört der anderen
        # CLI und ist dort wertlos. Lieber gleich frisch starten als in einen
        # Session-Fehler laufen — verloren geht nichts, build_prompt(resume=False) legt
        # den ganzen Faden als Text in den Prompt.
        resume_id = ""
    if resume_id and not _resume_handle_lives(resume_id, runner_of(model)):
        resume_id = ""
    journal = RunJournal(gc_id, title, base_url, timeout, model, journal_dir)
    if on_beat:  # Journal-Pfad sofort melden — der Stopp-Knopf braucht ihn ab Sekunde 1
        try:
            on_beat({"journal": str(journal.meta_path), "stop_path": str(journal.stop_path),
                     "steps": 0, "last_tool": "", "session_id": "", "rate_limit": ""})
        except Exception:  # noqa: BLE001
            pass
    # Git-Snapshot für Kern-Anchor UND optionale Quittung: SHA + bereits offene Dateien.
    # Kein try nötig — git_state degradiert bei Git-Fehlern auf leere Fakten.
    started, git_before = time.time(), _git.snapshot()

    # Frühere Fäden sind ein Preflight für DEN aktuellen Turn: lokale FTS-Kandidaten,
    # dann höchstens drei Leads durch ein billiges Modell derselben Provider-Familie.
    # Fehler sind ein No-op; der eigentliche Board-Agent muss immer starten.
    lane = runner_of(model)
    provider = "codex" if lane == "codex" else "claude"
    rerank_cmd = CODEX_CMD if provider == "codex" else claude_cmd
    codex_home = (_p.DATA / "codex-home"
                  if provider == "codex" else None)
    board_path = sidecar_dir.parent / "board.md"
    archive_path = sidecar_dir.parent / "board-archive.md"
    index_path = (_thread_search.INDEX_PATH if board_path.resolve() == _p.BOARD.resolve()
                  else sidecar_dir.parent / ".thread-search.sqlite")
    expanded_ask = _expand_ask(pending.get("last_ask", ""), sidecar_dir)
    retrieved_context, context_meta = _thread_search.context_for(
        pending, provider, rerank_cmd, board_path, archive_path, sidecar_dir, index_path,
        expanded_last_ask=expanded_ask, codex_home=codex_home,
        scope="all",
    )

    prompt = build_prompt(pending, resume=bool(resume_id), sidecar_dir=sidecar_dir,
                          runner=lane, retrieved_context=retrieved_context)
    journal.save_prompt(prompt)  # Observability: „🔍 Prompt anzeigen" im ⋯-Menü
    agent_env = {"GC_BOARD_URL": base_url}
    out = spawn_agent(prompt, resume_id, claude_cmd, timeout, model, journal=journal,
                      on_beat=on_beat, extra_env=agent_env)
    resumed = bool(resume_id)
    if not out["ok"] and not out.get("killed") and resume_id and _looks_like_dead_session(out):
        # Resume kann legitim wegbrechen (Session gelöscht/zu alt) → EIN frischer Versuch.
        # NUR bei Session-Fehlern — nach Abbruch/Crash hat der erste Lauf evtl. schon
        # gearbeitet; blind neu starten hieße Arbeit doppelt machen (SOL-Finding).
        # `killed` schließt das explizit aus: ein gedrückter Stopp-Knopf darf sich nicht
        # dadurch rächen, dass sofort ein frischer Lauf hinterherstartet.
        retry_prompt = build_prompt(pending, resume=False, sidecar_dir=sidecar_dir,
                                    runner=runner_of(model), retrieved_context=retrieved_context)
        journal.save_prompt(retry_prompt)  # der Retry-Prompt ist der, der wirklich lief
        out = spawn_agent(retry_prompt, "", claude_cmd, timeout, model, journal=journal,
                          on_beat=on_beat, extra_env=agent_env)
        resumed = False
        if out["ok"]:
            out["reply"] = "(new session — the old one could no longer be resumed) " + out["reply"]
    out["thread_context"] = context_meta
    if out.get("killed"):
        log_kill(gc_id, title, model, out["killed"], out.get("elapsed", 0),
                 out.get("beat", {}), journal.out_path)
    log_usage(gc_id, title, model, resumed, out, out_path=journal.out_path)
    # Receipt VOR dem Append: was der Runner gemessen hat, liegt damit auch dann auf
    # Platte, wenn der Append scheitert und der Run im Journal hängen bleibt.
    _receipt.write(gc_id, title, out, git_before, started)
    _anchor_save(gc_id, _git.snapshot())  # Bezugspunkt für den Git-Block des nächsten Turns

    text, session, gc_last = _outcome(out, gc_id, title, sidecar_dir)
    journal.ready(text, session, gc_last)  # ab hier ist die Antwort durabel — der Append darf scheitern
    try:
        _post_append(base_url, gc_id, text, session, gc_last)
    except RuntimeError as e:
        # Journal bleibt liegen — recover_journals() trägt beim nächsten Serverstart nach.
        print(f"gc_runner: {e} — reply is stored in journal {journal.meta_path.name}", file=sys.stderr)
        out["post_failed"] = True
        return out
    journal.discard()
    return out


def _fetch_pending(base_url: str, gc_id: str) -> dict | None:
    with urllib.request.urlopen(f"{base_url}/api/gc-pending", timeout=10) as r:
        for p in json.load(r)["pending"]:
            if p["addr"].get("id") == gc_id:
                return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="item @gc-id (must have for_gc status)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--model", default="", help="model alias (for example opus/sonnet); empty = CLI default")
    args = ap.parse_args()
    pending = _fetch_pending(args.url, args.id)
    if not pending:
        print(f"gc_runner: no pending item with id {args.id} (status must be for_gc)", file=sys.stderr)
        return 1
    out = run_item(pending, args.url, timeout=args.timeout, model=args.model)
    print(json.dumps({k: out[k] for k in ("ok", "session_id")} | {"reply_len": len(out["reply"])}))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
