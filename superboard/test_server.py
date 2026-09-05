#!/usr/bin/env python3
"""Tests für den todo-board-Server — Fokus auf die datenverlust-kritischen Pfade
(Round-Trip-Verlustfreiheit, lost-Guards) und das Board-Agent-Datenmodell
(@gc-id / @gc-session / gc-pending). Läuft NIE gegen die Live-board.md — echte
Daten nur read-only zur Regression, alles Schreibende auf Temp-Kopien.

    python3 test_server.py        # exit 0 = alle grün, 1 = Fehler
"""
from __future__ import annotations

import json
import re
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import board_lint
import gc_runner
import claude_identity
import receipt
import server

# The direct script path bypasses conftest.py. Synthetic test items must never leave the
# process for a real Haiku/Luna call; retrieval behavior has its own explicit tests.
os.environ.setdefault("GC_THREAD_CONTEXT", "0")

# Umbiegung auf MODULEBENE, nicht (nur) in main(): pytest ruft die test_*-Funktionen direkt
# und läuft an main() vorbei — die dortige Umbiegung griff dann nicht, und jeder pytest-Lauf
# schrieb Test-Journale ins echte journal/ und ~8 Fake-Zeilen ins echte usage-log.jsonl
# (real passiert 2026-07-20: 56 Müll-Zeilen um die ersten zwei Echtdaten herum).
gc_runner.JOURNAL_DIR = Path(tempfile.mkdtemp(prefix="gc-test-journal-"))
gc_runner.USAGE_LOG = gc_runner.JOURNAL_DIR / "usage-log-test.jsonl"
# Gleiche Falle, gleiche Lösung: der Wesen-Tages-Snapshot hängt am /api/cockpit-Aufruf,
# den der Endpoint-Test mit einem SYNTHETISCHEN Board auslöst — ohne Umbiegung landet
# eine Fantasie-Zeile in der echten Historie (real passiert 2026-07-21, direkt beim
# ersten Lauf nach Einbau). Die Historie ist die Datengrundlage fürs Nachjustieren
# der Schwellen — sie muss sauber bleiben.
server.WESEN_HISTORY = gc_runner.JOURNAL_DIR / "wesen-history-test.jsonl"
# Dritte Instanz derselben Falle: run_item() schreibt seit 2026-07-23 ein Run-Receipt.
# Ohne Umbiegung legt jeder Testlauf Fantasie-Receipts in das echte inbox/gc-receipts/
# — und die itemweise Retention löscht dabei fröhlich echte Receipts desselben Items.
receipt.RECEIPT_DIR = gc_runner.JOURNAL_DIR / "receipts-test"
# Vierte Instanz — und die einzige, die den Owner direkt anspringt: log_kill() schreibt beim
# Abbruch eines Runs nach journal/killed-runs.jsonl, und daraus baut das Board die Notiz
# „⚠ N Runs heute abgebrochen". test_interrupt_und_weiter stoppt einen ECHTEN Run, also hängte
# jeder Lauf eine Fantasie-Zeile „Offener Faden (von dir gestoppt, 0 min)" an; am 2026-07-30
# standen 19 davon im Board (Owner: „das stimmt gar nicht, die wurden nicht gerade abgebrochen").
# Zweiter, stillerer Schaden: KILL_LOG.parent trägt auch killed/, und dessen Retention hält nur
# KILL_KEEP=15 Ströme — die 92-Byte-Testkopien verdrängten die Ströme ECHTER Abbrüche.
# conftest.py biegt dasselbe um, das griff aber NUR unter pytest: `python3 test_server.py` und
# der direkte Funktionsaufruf laufen an conftest vorbei (verifiziert 2026-07-30 — Reproduktion
# schrieb prompt eine 16. Zeile ins Live-Log). Modulebene deckt beide Wege ab.
gc_runner.KILL_LOG = gc_runner.JOURNAL_DIR / "killed-runs-test.jsonl"
# Fünfte Umbiegung, aber die einzige, die von der Platte HEREIN wirkt statt hinaus:
# `server.RESTART_LOCK` ist der maschinenglobale Pfad /tmp/board-restart.lock. Solange ein
# `restart-server.sh` auf das Ende der laufenden Runs wartet (Drain, bis zu 40 min), lehnt
# `launch_gc_run` JEDEN Run-Start ab — und damit fielen am 12.08.2026 reproduzierbar 10–12
# Tests, alle über den Run-Spawn-Pfad („run: 202 accepted" & Folgechecks), ohne dass am Code
# etwas kaputt war. Ein Test darf nicht davon abhängen, ob gerade jemand das Board neustartet.
# Die Umbiegung zeigt auf einen Pfad, der nie existiert; wer den Drain-Pfad selbst testen
# will, legt sich seinen eigenen Lock an (s. test_drain_lehnt_run_ab).
server.RESTART_LOCK = gc_runner.JOURNAL_DIR / "kein-restart-lock"

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  OK  " if cond else " FAIL ") + name)
    if not cond:
        FAILS.append(name)


REAL_BOARD = Path(__file__).resolve().parents[2] / "inbox" / "board.md"


def _sidecar_from_ref(ref: str, board_path: Path) -> Path:
    """Sidecar-Verweis eines Test-Boards auflösen: außerhalb des Repos absolut,
    innerhalb des Repos wie im echten board.md relativ zur Repo-Wurzel."""
    path = Path(ref)
    return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path


def _serve(board_path: Path) -> tuple[ThreadingHTTPServer, int]:
    """Test-Server auf ephemerem Port (0 = OS wählt) — feste Ports kollidieren,
    sobald zwei Suites/Agenten parallel testen (real möglich auf diesem Repo)."""
    server.Handler.board_path = board_path
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]

SYNTH = """## Thema

### Jetzt

- [ ] Offener Faden *(2026-07-10)*
  Body-Zeile
  @gc-id: aaaaaaaaaaaa
  @gc: was ist LOMS?
  @gc-session: sess-uuid-a · board-was-ist-loms

- [ ] Offener Faden *(2026-07-10)*
  @gc-id: bbbbbbbbbbbb
  @gc: zweite frage, gleicher titel+datum

- [ ] Beantwortet *(2026-07-10)*
  @gc: frage
  @gc-re: antwort

- [ ] Geschlossen *(2026-07-10)*
  @gc: frage
  @gc-re: antwort
  @gc-done:

### Bald

### Geparkt

# Personen

# Notizen
"""


def test_real_board_regression() -> None:
    """Die echte board.md darf durch parse→serialize NIE Struktur/Inhalt verlieren.

    Skip statt Fehlschlag, wenn es keine echte board.md gibt, etwa in einer
    Source-Archiv-Kopie oder während eines isolierten Package-Builds.
    """
    if not REAL_BOARD.exists():
        import pytest
        pytest.skip(f"keine echte board.md unter {REAL_BOARD} (Kopie ohne inbox/)")
    text = REAL_BOARD.read_text()
    b1 = server.parse_board(text)
    check("real: parse→serialize→parse strukturgleich", b1 == server.parse_board(server.serialize_board(b1)))
    check("real: serialize ist Fixpunkt", server.serialize_board(b1) == server.serialize_board(server.parse_board(server.serialize_board(b1))))
    check("real: lost_boxes=0", server.lost_boxes(text, b1) == 0)
    check("real: lost_thread_events=0", server.lost_thread_events(text, b1) == 0)
    check("real: lost_session_lines=0", server.lost_session_lines(text, b1) == 0)
    check("real: lost_sessions_lines=0", server.lost_sessions_lines(text, b1) == 0)
    check("real: lost_id_lines=0", server.lost_id_lines(text, b1) == 0)


def test_version_und_changelog_synchron() -> None:
    """Internal build and public release versions each match their own history.

    Der Drift-Guard zur Konvention "jeder Code-Commit bumpt" (23.07., Faden
    6b525d987c57). Fängt beide Richtungen: Bump ohne Changelog-Eintrag und
    Changelog-Eintrag ohne Bump. Läuft nightly als Guard 11 mit — das ist die
    einzige Mechanik hinter der Regel, bewusst kein Commit-Hook.

    pyproject.toml wird bewusst NICHT gegen server.py verglichen (bis 2026-08-26 tat
    dieser Test das noch): das ist seither die OEFFENTLICHE Version, die nur bei einem
    bewussten Release bewegt wird, nicht bei jedem Commit. Ein Vergleich hier wuerde
    genau die Trennung wieder einreissen, die bump.py am 26.08. bekommen hat.
    """
    import re as _re
    here = Path(__file__).resolve().parent
    version = server.current_version()
    text = (here / "CHANGELOG.md").read_text(encoding="utf-8")
    m = _re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, _re.M)
    check("changelog: oberste Versions-Überschrift gefunden", m is not None)
    check(f"version: server.py {version} == CHANGELOG {m.group(1) if m else '?'}",
          m is not None and m.group(1) == version)
    project = (here.parent / "pyproject.toml").read_text(encoding="utf-8")
    pm = _re.search(r'^version = "([^"]+)"', project, _re.M)
    releases = (here.parent / "RELEASES.md").read_text(encoding="utf-8")
    rm = _re.search(r"^## \[(\d+\.\d+\.\d+)\]", releases, _re.M)
    check(f"version: public pyproject {pm.group(1) if pm else '?'} == RELEASES {rm.group(1) if rm else '?'}",
          pm is not None and rm is not None and pm.group(1) == rm.group(1))
    check("changelog: kein [Unreleased] mehr (abgeschafft 23.07.)", "## [Unreleased]" not in text)


def test_documentation_contract_im_agent_prompt() -> None:
    """Der event-getriebene Drift-Guard muss in Fresh- und Resume-Runs ankommen."""
    full = gc_runner.PROMPT_CONTRACT
    short = gc_runner.PROMPT_REMINDER
    for label, prompt in (("voll", full), ("resume", short)):
        check(f"docs-contract {label}: README-Rolle", "usage" in prompt and "README" in prompt)
        check(f"docs-contract {label}: Architektur-Rolle",
              "invariants" in prompt and "trust boundaries" in prompt and "ARCHITEKTUR" in prompt)
        check(f"docs-contract {label}: Changelog-Rolle", "code change" in prompt and "CHANGELOG" in prompt)
        check(f"docs-contract {label}: keine flüchtigen Fakten in Architektur",
              "temporary" in prompt)

def test_body_write_command_reaches_fresh_and_resume_prompts() -> None:
    """The safe write path has to reach every runner branch, with a fresh revision.

    An agent that hand-edits board.md can cost an item its @gc-id (see the
    known-limitations section of docs/USING-SUPERBOARD.md). The prompt therefore
    has to hand it the exact board_write command AND the revision token, or the
    documented safe path is only advice."""
    board = server.parse_board(SYNTH)
    item = server.find_item(board, {"id": "aaaaaaaaaaaa"})[0]
    pending = server.pending_entry("theme", "Dev", "Jetzt", item, board)
    expected_etag = server.item_body_etag(["Body-Zeile"])
    check("body-write prompt: pending carries the body revision",
          pending["body_etag"] == expected_etag)
    for label, resume in (("fresh", False), ("resume", True)):
        prompt = gc_runner.build_prompt(pending, resume=resume)
        check(f"body-write prompt {label}: helper + id + revision",
              ".superboard/board_write.py" in prompt and "aaaaaaaaaaaa" in prompt
              and expected_etag in prompt)
        check(f"body-write prompt {label}: hand edit explicitly ruled out",
              "Never edit board.md" in prompt or "never edit `board.md`" in prompt)
        check(f"body-write prompt {label}: the conflict path is visible",
              "HTTP 409" in prompt and "--show" in prompt)
    stage_hint = gc_runner._stage_hint(pending, "claude")
    check("stage prompt: endpoint helper instead of a hand-written @stage line",
          "board_write.py --id aaaaaaaaaaaa --stage" in stage_hint
          and "Never edit board.md for a stage" in stage_hint)
    check("stage prompt: route planning by complexity first",
          "genuinely multi-phase" in stage_hint
          and "early findings can redirect later work" in stage_hint)
    check("stage prompt: a linear task may proceed without a master plan",
          "plan · skip: linear task; plan lives in this thread" in stage_hint)
    for runner in ("claude", "codex"):
        runner_hint = gc_runner.build_prompt(pending, resume=False, runner=runner)
        check(f"stage prompt {runner}: keeps the durable plan requirement",
              "@stage: plan · <plan-path>" in runner_hint)


def test_stale_client_save_cannot_drop_server_items() -> None:
    """Ein Tab mit altem Stand koennte per Whole-Board-Save (409-Retry mit frischem etag)
    extern eingefuegte Items still vernichten. Der Server lehnt das ab, ausser der Client
    deklariert die Loeschung ausdruecklich (removedIds)."""
    tmp = tempfile.mktemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        stale = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
        # extern (Quick-Capture / Agent / anderer Tab) kommt ein neues Item dazu
        Path(tmp).write_text(Path(tmp).read_text().replace("### Bald\n", "### Bald\n\n- [ ] Extern eingefügt *(2026-08-25)*\n  @gc-id: cccccccccccc\n  @gc: neu von außen\n", 1))
        fresh_etag = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/etag"))["etag"]
        # stale Tab versucht Save mit FRISCHEM etag (genau der UI-Retry) → 409, Datei unangetastet
        before = Path(tmp).read_text()
        code, r = _post(port, "/api/board", {"board": stale["board"], "baseEtag": fresh_etag})
        check("stale-save: Save ohne das Server-Item → 409", code == 409 and "cccccccccccc" in r.get("missing", []))
        check("stale-save: Datei unangetastet", Path(tmp).read_text() == before)
        # bewusstes Löschen ist weiter erlaubt, wenn der Client es deklariert
        code, r = _post(port, "/api/board", {"board": stale["board"], "baseEtag": fresh_etag,
                                              "removedIds": ["cccccccccccc"]})
        check("stale-save: deklariertes Löschen → 200", code == 200 and "cccccccccccc" not in Path(tmp).read_text())
        source = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
        check("stale-save UI: ✕ deklariert removedIds", "removedIds.add(gone.id)" in source)
        check("stale-save UI: Save sendet removedIds", "removedIds: [...removedIds]" in source)
        check("stale-save UI: 409-Merge übernimmt Server-only Items", "Server-only Items" in source)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_ui_conflict_merge_preserves_external_body() -> None:
    """Ein stale Browser-Tab darf den frischen /api/gc-body-Write nicht klobbern."""
    source = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
    check("body-write UI: eigene Dirty-Menge existiert", "const bodyDirty = new Set()" in source)
    check("body-write UI: lokaler Body-Edit markiert dirty", "bodyDirty.add(it.id)" in source)
    check("body-write UI: 409-Merge übernimmt externen Body nur ohne lokalen Edit",
          "if (!bodyDirty.has(it.id)) it.body = srv.body || []" in source)
    check("body-write UI: erfolgreicher Save/Load räumt Dirty-Zustand",
          source.count("bodyDirty.clear()") >= 2)


def test_contract_split_byte_stable() -> None:
    """Phase 5: Kern + Instanzregeln ergeben EXAKT die autorisierte englische Fassung.

    Die SHA-256-Digests wurden unmittelbar nach der vollständigen Übersetzung aus den
    gerenderten Verträgen gemessen. Ein Zeichen Drift macht den Test rot; zusätzlich
    exerzieren wir fresh/resume/compact und den generischen Ohne-Instanz-Fallback.
    """
    import hashlib
    import contract

    # Nachgemessen 2026-08-17 (Faden 7921a2f15c8b "agent todo", Runde 2): Sub-Step-Listen-
    # Regel auf Wunsch des Owners zur EIN-Zeilen-Empfehlung verkürzt — AUTORISIERT [Owner].
    # Re-measured after the open-source extraction (2026-08-18): the shipped repo has no
    # board.contract.md, so only the generic core rules render (full.docs/full.git restored
    # here — they were accidentally dropped during de-personalization, not deliberately
    # instance-only; see ARCHITEKTUR.md/CHANGELOG.md added in the same pass).
    # Re-measured 2026-08-24 for the release candidate. The contract grew by three
    # AUTHORIZED additions ported in the same pass: the session-bound credential case
    # in the auth-handoff rule, the working-state update via `board_write.py` instead
    # of a hand edit, and the optional demo slots. Nothing was removed.
    # Re-measured 2026-08-24, second pass (first-run hardening). Two AUTHORIZED
    # changes: a new `board_client` block naming the workspace client and its full
    # verb set — without it agents were told "never edit board.md" while holding no
    # tool that reliably exists — and `full.docs` no longer ASSERTS that the
    # workspace has README/ARCHITEKTUR (a Superboard workspace normally does not).
    # Re-measured 2026-09-05 for the 0.3.0 port. ONE authorized change to the rendered
    # text: `full.board_client` now names the column choices `Now|Next|Backlog`, which is
    # what `board_write.py --col` actually accepts — the old `Jetzt|Bald|Geparkt` were the
    # INTERNAL dict keys and would have been rejected by the client (-2 bytes). The port
    # also adds `full.reply_style` to `_FULL_ORDER`, but the block text lives in an
    # instance `board.contract.md`; a shipped Superboard has none, so it renders nothing.
    snapshots = {
        "full": (5741, "25c8b042e72763912f09486d12df89959a2e5686dc0083db20736219ba9a88ff"),
        "reminder": (1364, "9e44a266f5808cf0b119549680078559c2b1a6e23f1acf32769451620d0c3027"),
    }
    for kind, (length, digest) in snapshots.items():
        rendered = contract.render(kind)
        check(f"contract {kind}: Länge wie Phase 2", len(rendered) == length)
        check(f"contract {kind}: byteweise wie Phase 2",
              hashlib.sha256(rendered.encode()).hexdigest() == digest)

    check("contract: Runner nutzt den gerenderten Vollvertrag",
          gc_runner.PROMPT_CONTRACT == contract.render("full"))
    check("contract: Runner nutzt den gerenderten Reminder",
          gc_runner.PROMPT_REMINDER == contract.render("reminder"))

    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "fehlt.md"
        core = contract.render("full", missing, owner="You")
        check("contract: ohne Instanzdatei bleibt der Faden-Kern",
              "@gc-re:" in core and "### Working state" in core
              and "Authentication boundary" in core)
        check("contract: ohne Instanzdatei fehlen die Hausregeln der origin instance",
              "DECISIONS" not in core and "git push" not in core
              and "Learning Capture" not in core)

    pending = {"addr": {"id": "contracttest", "name": "T", "col": "Jetzt"},
               "title": "Vertrag", "body": [], "session": "",
               "thread": [{"kind": "ask", "text": "prüfen"}], "last_ask": "prüfen"}
    fresh = gc_runner.build_prompt(pending, resume=False)
    resumed = gc_runner.build_prompt(pending, resume=True)
    pending["gc_last"] = "kompaktiert · 2026-08-13 12:00"
    compacted = gc_runner.build_prompt(pending, resume=True)
    private_full = gc_runner._contract_for("claude", "full")
    private_reminder = gc_runner._contract_for("claude", "reminder")
    check("contract: fresh trägt Vollfassung",
          private_full in fresh and private_reminder not in fresh)
    check("contract: resume trägt Reminder",
          private_reminder in resumed and private_full not in resumed)
    check("contract: nach Compact wieder Vollfassung",
          private_full in compacted and private_reminder not in compacted)


def test_contract_protocol_markers_remain_frozen() -> None:
    """Phase 5: englischer Vertrag darf das persistierte deutsche Protokoll nicht übersetzen."""
    import contract
    import markers

    full = contract.render("full")
    check("markers: Faden-Tags bytegleich",
          markers.GC_TAG == {"ask": "@gc:", "reply": "@gc-re:",
                             "done": "@gc-done:", "sys": "@gc-sys:"})
    check("markers: Sidecar-Labels bytegleich",
          markers.REF_LABEL == {"ask": "full text", "brief": "full text",
                                "reply": "full reply", "done": "full text"})
    for label in set(markers.REF_LABEL.values()):
        sample = f"→ {label}: inbox/gc-threads/abc-123.md"
        match = markers.REF_RE.search(sample)
        check(f"markers: REF_RE versteht {label}",
              match is not None and match.group(1) == "abc-123.md")
        check(f"markers: Vertrag nennt {label}", f"→ {label}: …" in full)
    sidecar = "prefix inbox/gc-threads/abc-123.md suffix"
    match = markers.SIDECAR_REF_RE.search(sidecar)
    check("markers: SIDECAR_REF_RE bleibt kompatibel",
          match is not None and match.group(1) == "abc-123.md")
    check("markers: Compact-Präfix bytegleich", markers.COMPACTED_PREFIX == "kompaktiert")
    check("markers: Handoff-Präfix bytegleich und im Vertrag",
          markers.HANDOFF_PREFIX == "🔑 CLI-Handoff nötig:"
          and markers.HANDOFF_PREFIX in full)


def test_bump_entscheidet_minor_vs_patch() -> None:
    """bump.py: fix bleibt patch, feat steigt mit der Größe, Flags übersteuern.

    Schwellen: minor ab MINOR_LINES (seit 18.08. 200, vorher 100). MAJOR faellt seit
    18.08. NIE automatisch — ab MAJOR_HINT_LINES schlaegt das Skript eine Hauptnummer
    nur vor (Faden `51849ed8344e`: "Lieber zählt die munter nach oben, als dass wir
    hier eine komplizierte Regel einbauen"). Die Tests hängen an den Konstanten,
    nicht an den Zahlen — wer die Schwelle verschiebt, soll keine Tests nachziehen.
    """
    import bump
    check("bump: fix ist immer patch (auch groß)", bump.decide("fix(gc): x", 400, None)[0] == "patch")
    check("bump: fix bleibt auch jenseits des major-Hinweises patch",
          bump.decide("fix(gc): x", bump.MAJOR_HINT_LINES + 1, None)[0] == "patch")
    check("bump: feat unter Schwelle = patch", bump.decide("feat(ui): x", 11, None)[0] == "patch")
    check("bump: feat knapp unter MINOR_LINES bleibt patch",
          bump.decide("feat(ui): x", bump.MINOR_LINES - 1, None)[0] == "patch")
    check("bump: feat ab MINOR_LINES = minor", bump.decide("feat(ui): x", bump.MINOR_LINES, None)[0] == "minor")
    check("bump: feat bleibt minor, auch weit über dem major-Hinweis",
          bump.decide("feat(ui): x", bump.MAJOR_HINT_LINES * 2, None)[0] == "minor")
    check("bump: major nur per Flag", bump.decide("feat(ui): x", 10, "major")[0] == "major")
    check("bump: --patch übersteuert feat", bump.decide("feat(ui): x", 999, "patch")[0] == "patch")
    check("bump: der major-Hinweis liegt über der minor-Schwelle",
          bump.MINOR_LINES < bump.MAJOR_HINT_LINES)
    check("bump: patch zählt rechts hoch", bump.bump((0, 13, 0), "patch") == "0.13.1")
    check("bump: minor zählt Mitte hoch, patch auf 0", bump.bump((0, 13, 4), "minor") == "0.14.0")
    check("bump: major zählt links hoch, Rest auf 0", bump.bump((0, 15, 2), "major") == "1.0.0")


def test_bump_zaehlt_ersetzte_zeile_einmal() -> None:
    """bump.py: `max(added, deleted)` je Datei — eine ersetzte Zeile ist EINE Zeile.

    13.08. (Faden `6aa4dbc3a873`, Frage 2 → A): die alte Summe `added + deleted`
    bewertete jeden 1:1-Rewrite doppelt, weshalb die Englisch-Umstellung als 2.526
    statt 1.361 Zeilen gemessen wurde. Der Test hängt am realen numstat-Format,
    inklusive Binär- ("-") und Nicht-Code-Zeilen, die gar nicht zählen dürfen.
    """
    import bump
    total, files = bump.numstat_churn("632\t633\tsuperboard/index.html\n")
    check("bump: 1:1-Ersetzung zählt einmal", total == 633)
    check("bump: Datei wird benannt", files == ["index.html"])
    check("bump: reines Hinzufügen zählt voll",
          bump.numstat_churn("154\t0\tsuperboard/test_x.py\n")[0] == 154)
    check("bump: reines Löschen zählt voll",
          bump.numstat_churn("0\t80\tsuperboard/alt.py\n")[0] == 80)
    check("bump: mehrere Dateien summieren sich",
          bump.numstat_churn("10\t10\ta/x.py\n5\t2\ta/y.py\n")[0] == 15)
    check("bump: Doku zählt nicht mit",
          bump.numstat_churn("900\t900\tsuperboard/CHANGELOG.md\n")[0] == 0)
    check("bump: Binärdateien zählen nicht mit",
          bump.numstat_churn("-\t-\tsuperboard/icon.png\n")[0] == 0)


def test_meta_lines_roundtrip() -> None:
    """@gc-id / @gc-session / thread nebeneinander — verlustfrei, keine Regex-Kollision."""
    sb = server.parse_board(SYNTH)
    items = [it for _s, _n, _c, it in server._all_items(sb)]
    check("synth: 4 Items", len(items) == 4)
    check("synth: id geparst", items[0]["id"] == "aaaaaaaaaaaa")
    check("synth: session neben id", items[0]["session"] == "sess-uuid-a · board-was-ist-loms")
    check("synth: @gc-id/@gc-session NICHT als thread-Event", [e["kind"] for e in items[0]["thread"]] == ["ask"])
    check("synth: body erhalten", items[0]["body"] == ["Body-Zeile"])
    check("synth: alle lost-Guards = 0",
          server.lost_boxes(SYNTH, sb) == 0 and server.lost_thread_events(SYNTH, sb) == 0
          and server.lost_session_lines(SYNTH, sb) == 0 and server.lost_id_lines(SYNTH, sb) == 0)
    check("synth: voller round-trip identisch", server.parse_board(server.serialize_board(sb)) == sb)


def test_multiline_body_element_survives_serialization() -> None:
    """Ein Body-Element MIT Zeilenumbruch (Action-Prompt aus actions.json) muss
    zeilenweise eingerueckt rausgehen. Vorher landete alles ab Absatz 2 auf Spalte 0,
    war unparsebar und wurde beim naechsten Save geloescht — und blockte, solange es
    im Board lag, ueber `lost_total() > 0` jeden Schreibpfad mit HTTP 409
    (gemessen 2026-08-26 an der Teams-Process-Action)."""
    sb = server.parse_board(SYNTH)
    it = [x for _s, _n, _c, x in server._all_items(sb)][0]
    it["body"] = ["action:demo", "\u00b7\u00b7\u00b7", "Absatz eins.\n\nAbsatz zwei.\n\nAbsatz drei."]
    text = server.serialize_board(sb)
    check("multiline body: keine Zeile auf Spalte 0",
          all(not re.match(r"^Absatz", ln) for ln in text.split("\n")))
    again = server.parse_board(text)
    it2 = [x for _s, _n, _c, x in server._all_items(again)][0]
    check("multiline body: alle drei Absaetze erhalten",
          [b for b in it2["body"] if b.startswith("Absatz")]
          == ["Absatz eins.", "Absatz zwei.", "Absatz drei."])
    check("multiline body: nichts verloren", server.lost_total(text, again) == 0)
    check("multiline body: zweiter round-trip stabil",
          server.parse_board(server.serialize_board(again)) == again)


LEGACY_HEADS_SYNTH = """## Thema

### Jetzt

- [ ] Altes Item *(2026-08-01)*
  @gc-id: 1a1a1a1a1a1a

### Wartet auf andere

- [ ] Wartend *(2026-08-01)*
  @gc-id: 1b1b1b1b1b1b
  @wait: alex

### Bald

### Geparkt

# Personen

## Hans → personal/people/hans.md

- [ ] Meeting-Punkt *(2026-08-01)*
  @gc-id: 1c1c1c1c1c1c

# Notizen

Freier Text.
"""

ENGLISH_HEADS_SYNTH = """## Thema

### Now

- [ ] Neues Item *(2026-08-18)*
  @gc-id: 2a2a2a2a2a2a

### Waiting on others

- [ ] Wartend *(2026-08-18)*
  @gc-id: 2b2b2b2b2b2b
  @wait: alex

### Next

### Backlog

# To discuss

## Hans → personal/people/hans.md

- [ ] Meeting-Punkt *(2026-08-18)*
  @gc-id: 2c2c2c2c2c2c

# Notes

Freier Text.
"""


def test_english_headings_file_boundary() -> None:
    """Datei-Grenz-Übersetzung: board.md muss Englisch sein (`### Now|
    Next|Backlog|Waiting on others|Dates`, `# To discuss`/`# Notes`), die INTERNEN
    Dict-Keys bleiben legacy Deutsch (`theme["cols"]["Jetzt"]` etc. — Identifier, kein
    UI-Text). column_key()/section_key() sind die einzige Normalisierungsstelle.

    Drei Eigenschaften müssen gleichzeitig gelten, sonst bricht entweder ein altes
    Hand-Edit (Alt-Datei parst nicht mehr) oder das neue Format (Save schreibt wieder
    Deutsch):
    1. eine Alt-Datei mit deutschen Überschriften parst verlustfrei,
    2. serialize_board schreibt IMMER Englisch — auch aus einem frisch geparsten
       Alt-Board,
    3. der Round-Trip legacy → parse → serialize → parse ist strukturell stabil
       (gleiche Themen/Personen/Items/Guards), auch wenn der Text sich ändert."""
    # (1) Alt-Datei parst verlustfrei — Spalten UND Sektionen richtig zugeordnet.
    legacy = server.parse_board(LEGACY_HEADS_SYNTH)
    check("legacy: lost-Guards = 0", server.lost_total(LEGACY_HEADS_SYNTH, legacy) == 0)
    check("legacy: Jetzt-Item geparst", legacy["themes"][0]["cols"]["Jetzt"][0]["id"] == "1a1a1a1a1a1a")
    check("legacy: Wartet-Item geparst",
          legacy["themes"][0]["cols"]["Wartet auf andere"][0]["id"] == "1b1b1b1b1b1b")
    check("legacy: Personen-Item geparst", legacy["persons"][0]["items"][0]["id"] == "1c1c1c1c1c1c")
    check("legacy: Notizen-Freitext geparst", legacy["notes"] == ["Freier Text."])

    # (2) serialize_board schreibt IMMER Englisch, egal ob aus Alt- oder Neu-Board geparst.
    out_from_legacy = server.serialize_board(legacy)
    check("legacy->serialize: keine deutsche Spalten-Überschrift mehr",
          "### Jetzt" not in out_from_legacy and "### Wartet auf andere" not in out_from_legacy
          and "### Bald" not in out_from_legacy and "### Geparkt" not in out_from_legacy)
    check("legacy->serialize: keine deutsche Sektions-Überschrift mehr",
          "# Personen" not in out_from_legacy and "# Notizen" not in out_from_legacy)
    check("legacy->serialize: englische Überschriften stehen da",
          "### Now" in out_from_legacy and "### Waiting on others" in out_from_legacy
          and "# To discuss" in out_from_legacy and "# Notes" in out_from_legacy)

    # Eine bereits-englische Datei parst genauso verlustfrei (die zweite akzeptierte
    # Schreibweise) und bleibt beim Serialisieren Englisch (kein Zurückfallen).
    english = server.parse_board(ENGLISH_HEADS_SYNTH)
    check("english: lost-Guards = 0", server.lost_total(ENGLISH_HEADS_SYNTH, english) == 0)
    check("english: Jetzt-Item (interner Key bleibt Deutsch) geparst",
          english["themes"][0]["cols"]["Jetzt"][0]["id"] == "2a2a2a2a2a2a")
    check("english->serialize: bleibt Englisch",
          "### Now" in server.serialize_board(english)
          and "### Jetzt" not in server.serialize_board(english))

    # (3) Round-Trip legacy -> parse -> serialize -> parse: strukturell stabil.
    reparsed = server.parse_board(out_from_legacy)
    check("legacy round-trip: gleiche Themen-/Spalten-/Item-Struktur", reparsed == legacy)
    check("legacy round-trip: lost-Guards bleiben 0",
          server.lost_total(out_from_legacy, reparsed) == 0)
    check("legacy round-trip: gleiche Themen-/Personen-/Item-Zahl",
          (len(reparsed["themes"]), len(reparsed["persons"]),
           sum(1 for _ in server._all_items(reparsed)))
          == (len(legacy["themes"]), len(legacy["persons"]),
              sum(1 for _ in server._all_items(legacy))))


def test_thread_status() -> None:
    sb = server.parse_board(SYNTH)
    items = [it for _s, _n, _c, it in server._all_items(sb)]
    check("status: ask → for_gc", server.thread_status(items[0]) == "for_gc")
    check("status: reply → for_owner", server.thread_status(items[2]) == "for_owner")
    check("status: done → closed", server.thread_status(items[3]) == "closed")
    check("status: leerer Faden → none", server.thread_status({"thread": []}) == "none")


def test_find_item_by_id() -> None:
    """@gc-id löst den 409-Fall: zwei Items gleichen Titels+Datums eindeutig trennen."""
    sb = server.parse_board(SYNTH)
    a = server.find_item(sb, {"id": "aaaaaaaaaaaa"})
    b = server.find_item(sb, {"id": "bbbbbbbbbbbb"})
    check("find: id A eindeutig", len(a) == 1 and a[0]["thread"][0]["text"] == "was ist LOMS?")
    check("find: id B eindeutig", len(b) == 1 and "zweite frage" in b[0]["thread"][0]["text"])
    fp = server.find_item(sb, {"scope": "theme", "name": "Thema", "col": "Jetzt",
                               "title": "Offener Faden", "date": "2026-07-10"})
    check("find: Fingerprint-Fallback findet BEIDE (der alte 409-Fall)", len(fp) == 2)
    check("find: leere id fällt auf Fingerprint zurück (kein Match auf id='')",
          len(server.find_item(sb, {"id": "", "scope": "theme", "name": "Thema", "col": "Bald",
                                    "title": "x", "date": ""})) == 0)


def test_ensure_ids() -> None:
    noid = server.parse_board("## T\n\n### Jetzt\n\n- [ ] Ohne ID *(2026-07-10)*\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    it0 = [it for _s, _n, _c, it in server._all_items(noid)][0]
    check("ensure_ids: vorher leer", it0["id"] == "")
    server.ensure_ids(noid)
    gid = it0["id"]
    check("ensure_ids: 12 hex vergeben", len(gid) == 12 and all(c in "0123456789abcdef" for c in gid))
    server.ensure_ids(noid)
    check("ensure_ids: idempotent", it0["id"] == gid)
    check("ensure_ids: round-trip stabil", server.parse_board(server.serialize_board(noid)) == noid)


def test_arbeitsstand_dropped_on_done() -> None:
    """Abhaken räumt den Arbeitsstand-Block weg (2026-07-22, Blatt Q3=A) —
    aber nur beim Übergang offen→erledigt, und nur diesen einen Block. Seit
    2026-07-27 wandert er vorher ins Rohlager, statt ersatzlos zu sterben."""
    md = ("## T\n\n### Jetzt\n\n- [ ] X *(2026-07-10)*\n"
          "  Kurzkontext bleibt.\n  ···\n  Deep-Dive bleibt auch.\n"
          "  ### Arbeitsstand\n  Ziel: irgendwas\n  Stand: halb\n"
          "  @gc-id: aaaaaaaaaaaa\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    # Archiv aufs Temp-Verzeichnis umbiegen — sonst schreibt jeder Testlauf ins
    # echte logs/dreaming/ und verseucht das Rohlager mit Fixture-Müll.
    orig_archiv = server.ARBEITSSTAND_ARCHIV
    with tempfile.TemporaryDirectory() as td:
        server.ARBEITSSTAND_ARCHIV = Path(td) / "dreaming" / "archiv.md"
        try:
            disk = server.parse_board(md)
            incoming = server.parse_board(md.replace("- [ ] X", "- [x] X"))
            n = server.drop_arbeitsstand_on_done(disk, incoming)
            body = [it for _s, _n, _c, it in server._all_items(incoming)][0]["body"]
            check("arbeitsstand: ein Item getroffen", n == 1)
            check("arbeitsstand: Block weg",
                  not any("Arbeitsstand" in b or "Ziel:" in b for b in body))
            check("arbeitsstand: übriger Body unangetastet",
                  body == ["Kurzkontext bleibt.", "···", "Deep-Dive bleibt auch."])
            # Rohlager: der Block darf nicht verloren gehen, samt Herkunft.
            archiv = server.ARBEITSSTAND_ARCHIV.read_text(encoding="utf-8")
            check("arbeitsstand: Inhalt im Rohlager", "Ziel: irgendwas" in archiv
                  and "Stand: halb" in archiv)
            check("arbeitsstand: Herkunft im Rohlager",
                  "aaaaaaaaaaaa" in archiv and "X" in archiv)
            check("arbeitsstand: Deep-Dive NICHT mitarchiviert",
                  "Deep-Dive bleibt auch" not in archiv)
            # War das Item schon vorher erledigt, bleibt alles liegen — sonst radiert
            # ein beliebiger Re-Save Hand-Edits am Body weg (und archiviert doppelt).
            done_disk = server.parse_board(md.replace("- [ ] X", "- [x] X"))
            again = server.parse_board(md.replace("- [ ] X", "- [x] X"))
            check("arbeitsstand: kein Re-Save-Radierer",
                  server.drop_arbeitsstand_on_done(done_disk, again) == 0)
            check("arbeitsstand: kein Doppel-Archiv-Eintrag",
                  archiv == server.ARBEITSSTAND_ARCHIV.read_text(encoding="utf-8"))
        finally:
            server.ARBEITSSTAND_ARCHIV = orig_archiv
    # Ohne Block: Body bleibt identisch, inkl. abschließender ···-Zeile.
    plain = ["nur Text", "···"]
    check("arbeitsstand: ohne Block unverändert", server.strip_arbeitsstand(plain) == plain)
    check("arbeitsstand: extract ohne Block leer", server.extract_arbeitsstand(plain) == [])
    # extract und strip sind Gegenstücke: zusammen ergeben sie den Ausgangs-Body.
    full = ["Kurz.", "···", "### Arbeitsstand", "Ziel: a", "Gelernt: b"]
    check("arbeitsstand: extract liefert genau den Block",
          server.extract_arbeitsstand(full) == ["Ziel: a", "Gelernt: b"])
    check("arbeitsstand: unschreibbares Archiv wirft nicht",
          server.archive_arbeitsstand("t", "i", []) is False)


def test_prompt_mitschnitt_ueberlebt_discard() -> None:
    """Der Prompt-Mitschnitt liegt NEBEN dem Journal — discard() (läuft nach jedem
    erfolgreichen Append) darf ihn nicht mitnehmen, sonst ist er genau dann weg,
    wenn der Owner nachschaut. Plus: Retention hält nur die letzten 3 je Item."""
    with tempfile.TemporaryDirectory() as td:
        jd = Path(td)
        for i in range(5):
            j = gc_runner.RunJournal("aaaaaaaaaaaa", "T", "http://x", 60, "", jd)
            j.save_prompt(f"prompt nr {i}")
            j.discard()
        # Bewusst über prompt_files() statt sorted(glob(...)): der Dateiname endet auf
        # einem Zufallssuffix, gleichsekündige Runs ordnete die Namenssortierung nach
        # Zufall. Der Test prüfte damit dieselbe kaputte Reihenfolge, die er absichern
        # sollte, und war eine ~20-%-Flake (2026-07-22).
        kept = gc_runner.prompt_files(jd / "prompts", "aaaaaaaaaaaa")
        check("prompt: überlebt discard()", len(kept) > 0)
        check("prompt: Retention hält 3", len(kept) == gc_runner.RunJournal.PROMPT_KEEP)
        check("prompt: jüngster ist der letzte Run", kept[-1].read_text() == "prompt nr 4")
        check("prompt: Retention wirft die ÄLTESTEN weg, nicht beliebige",
              [p.read_text() for p in kept] == ["prompt nr 2", "prompt nr 3", "prompt nr 4"])
        # Fremde Items bleiben unangetastet.
        j2 = gc_runner.RunJournal("bbbbbbbbbbbb", "T", "http://x", 60, "", jd)
        j2.save_prompt("anderes item")
        check("prompt: Retention itemweise",
              len(list((jd / "prompts").glob("run-*.prompt.txt"))) == gc_runner.RunJournal.PROMPT_KEEP + 1)


def test_receipt_fakten_und_retention() -> None:
    """Run-Receipt (2026-07-22, Item a99928929814): der Runner protokolliert Fakten,
    die der Agent weder schönen noch vergessen kann. Kernbedingung des Owners: „wenn es da
    ist, ist es da, wenn nicht, nicht" — das Feature darf einen Run NIE kosten."""
    ok_out = {"ok": True, "denials": [{"tool_name": "Bash", "tool_input": {"command": "git push"}}],
              "context_tokens": 186000, "session_id": "abc-123", "raw_error": "",
              "usage_summary": {"cost_usd": 3.78, "duration_ms": 95000, "num_turns": 7,
                                "cache_hit_pct": 91, "models": ["claude-opus-4-8"]}}
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        p = receipt.write("aaaaaaaaaaaa", "Testitem", ok_out, "", time.time(), rd)
        txt = p.read_text()
        check("receipt: Datei entsteht", p.exists())
        check("receipt: Kosten drin", "$3.78" in txt)
        check("receipt: Kontextgröße drin", "~186k" in txt)
        # Der eigentliche Zugewinn: im Faden steht nur die ANZAHL geblockter Aktionen.
        check("receipt: geblockte Aktion im Detail", "git push" in txt and "Bash" in txt)
        check("receipt: Parallel-Sessions-Vorbehalt steht drin", "parallel" in txt.lower())

        # Fehllauf: der Grund gehört INS Receipt (im Stempel wollte der Owner ihn ausdrücklich nicht).
        bad = {"ok": False, "denials": [], "context_tokens": 0, "session_id": "",
               "raw_error": "subtype=error_max_turns", "usage_summary": {}}
        b = receipt.write("aaaaaaaaaaaa", "Testitem", bad, "", time.time(), rd)
        check("receipt: Fehllauf nennt den Grund", "error_max_turns" in b.read_text())

        # Retention itemweise, ÄLTESTE fliegen — dieselbe mtime-Falle wie bei prompt_files.
        for _ in range(receipt.KEEP_PER_ITEM + 3):
            receipt.write("aaaaaaaaaaaa", "Testitem", ok_out, "", time.time(), rd)
        receipt.write("bbbbbbbbbbbb", "Anderes", ok_out, "", time.time(), rd)
        check("receipt: Retention hält KEEP_PER_ITEM",
              len(receipt.receipt_files(rd, "aaaaaaaaaaaa")) == receipt.KEEP_PER_ITEM)
        check("receipt: Retention itemweise", len(receipt.receipt_files(rd, "bbbbbbbbbbbb")) == 1)

        # „Beeinflusst den Agenten in keiner Weise": kaputte Eingaben dürfen nicht werfen.
        check("receipt: wirft nie (kaputtes out-Dict)",
              receipt.write("cccccccccccc", "X", {"ok": True}, "", time.time(), rd) is not None)
        check("receipt: unschreibbares Verzeichnis → None statt Exception",
              receipt.write("dddddddddddd", "X", ok_out, "", time.time(), Path("/proc/nope")) is None)

        # Rückbau-Schalter: ein Flag, und es entstehen keine Dateien mehr.
        receipt.ENABLED = False
        try:
            check("receipt: ENABLED=False schreibt nichts",
                  receipt.write("eeeeeeeeeeee", "X", ok_out, "", time.time(), rd) is None
                  and not receipt.receipt_files(rd, "eeeeeeeeeeee"))
        finally:
            receipt.ENABLED = True


def test_receipt_dateien_vollstaendig_und_zugeordnet() -> None:
    """Bens Beschwerde 2026-07-23: „nicht alle angefassten Dateien drin, nur die ersten
    paar und dann ist das abgeschnitten." Zwei Fehler in einem: die Liste war auf 6
    gekappt UND zeigte den ganzen Repo-Dreck (70+ Dateien paralleler Sessions), nicht die
    Dateien DIESES Runs. Fix: snapshot() merkt sich den Vorher-Stand, das Receipt listet
    den eigenen Anteil vollständig und kollabiert den Fremdanteil auf eine Zahl."""
    fake = {"ok": True, "denials": [], "session_id": "x", "usage_summary": {}}

    # 12 eigene + 60 fremde offene Dateien: alle 12 müssen dastehen, die 60 nur als Zahl.
    delta = {"head": "b", "commits": [], "diffstat": "",
             "dirty": [f" M eigen{i}.md" for i in range(12)] + [f" M fremd{i}.md" for i in range(60)],
             "dirty_new": [f"eigen{i}.md" for i in range(12)], "dirty_pre": 60}
    txt = receipt._fmt_facts("a" * 12, "T", fake, delta, time.time())
    check("receipt: alle eigenen Dateien gelistet (nicht nach 6 abgeschnitten)",
          all(f"eigen{i}.md" in txt for i in range(12)))
    check("receipt: Fremdanteil als Zahl statt Liste",
          "60 file(s)" in txt and "fremd0.md" not in txt)

    # Cap greift erst bei LIST_MAX — und sagt dann, wie viel fehlt (vorher stiller Cut).
    many = {"head": "b", "commits": [], "diffstat": "", "dirty": [],
            "dirty_new": [f"f{i}.md" for i in range(receipt.LIST_MAX + 7)], "dirty_pre": 0}
    txt2 = receipt._fmt_facts("a" * 12, "T", fake, many, time.time())
    check("receipt: Cut wird beziffert, nicht verschwiegen", "… 7 more" in txt2)

    # Zuordnung end-to-end gegen echtes git — aber in einem WEGWERF-Repo, nicht im echten.
    # Erster Anlauf maß die origin instance selbst und war prompt rot: zwischen snapshot() und
    # git_delta() hatte eine parallele Board-Session eine Datei angefasst, die dann als
    # „neu seit Run-Start" galt. Genau die Race, die das Feature beschreibt — als Test
    # taugt sie nicht. (Allein grün, in der 28s-Suite rot: der Klassiker.)
    with tempfile.TemporaryDirectory() as gd:
        repo = Path(gd)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True)
        run("init", "-q")
        (repo / "alt.txt").write_text("a")
        run("add", "-A"); run("commit", "-qm", "start")
        (repo / "alt.txt").write_text("geaendert")     # VOR dem Lauf offen
        orig = receipt.GC_ROOT
        receipt.GC_ROOT = repo
        try:
            before = receipt.snapshot()
            check("receipt: Vorher-Stand wird erfasst", before["dirty"] == ["alt.txt"])
            (repo / "neu.txt").write_text("n")         # WÄHREND des Laufs entstanden
            d = receipt.git_delta(before)
            check("receipt: nur die Datei des Runs gilt als neu", d.get("dirty_new") == ["neu.txt"])
            check("receipt: der Altstand zählt als fremd", d.get("dirty_pre") == 1)

            # Alt-Pfad (blanker SHA statt snapshot-Dict): weiter gültig, nur ohne Zuordnung.
            legacy = receipt.git_delta(receipt.git_head())
            check("receipt: SHA-String bleibt gültige Eingabe",
                  "dirty" in legacy and "dirty_new" not in legacy)
        finally:
            receipt.GC_ROOT = orig


def test_journal_wache_verschont_laufenden_run() -> None:
    """Die Journal-Wache darf einem LEBENDEN Run das Journal nicht wegräumen.

    Regression zum Crash vom 23.07. (Faden 6b525d987c57): zwei Runs nacheinander
    auf demselben Item — sobald Run A antwortet, ist das Item nicht mehr @gc:-pending,
    und der noch laufende Run B fiel in den "nicht mehr offen → aufräumen"-Zweig. Ihm
    wurde die .out.json unter den Füßen gelöscht → "❌ Runner-Crash: [Errno 2]".
    """
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        jdir = Path(td)
        orig_open = gc_runner._open_gc_ids
        gc_runner._open_gc_ids = lambda _url: set()   # Item ist NICHT mehr pending
        try:
            # Run B: läuft noch, pid = dieser Testprozess (garantiert lebendig)
            j = gc_runner.RunJournal("bbbbbbbbbbbb", "läuft noch", "http://x", 900, journal_dir=jdir)
            j.set_pid(os.getpid())
            j.out_path.write_text('{"result": "halbfertig"}')
            gc_runner.recover_journals("http://x", journal_dir=jdir, sidecar_dir=jdir)
            check("wache: Journal des lebenden Runs bleibt", j.meta_path.exists() and j.out_path.exists())

            # Gegenprobe: toter Run, älter als RECOVER_GRACE → darf weg
            j2 = gc_runner.RunJournal("cccccccccccc", "tot", "http://x", 900, journal_dir=jdir)
            j2.meta["pid"] = 999999            # mit an Sicherheit grenzender Wahrscheinlichkeit tot
            j2.meta["started"] = time.time() - gc_runner.RECOVER_GRACE - 10
            j2._write()
            j2.out_path.write_text("{}")
            gc_runner.recover_journals("http://x", journal_dir=jdir, sidecar_dir=jdir)
            check("wache: totes Journal wird weiter aufgeräumt", not j2.meta_path.exists())

            # Startfenster: pid noch nicht gesetzt, frisch → auch nicht anfassen
            j3 = gc_runner.RunJournal("dddddddddddd", "startet gerade", "http://x", 900, journal_dir=jdir)
            gc_runner.recover_journals("http://x", journal_dir=jdir, sidecar_dir=jdir)
            check("wache: frisches Journal ohne pid bleibt", j3.meta_path.exists())
        finally:
            gc_runner._open_gc_ids = orig_open


def test_fehllauf_stempel() -> None:
    """Q3 (2026-07-22): auch abgebrochene Runs stempeln — vorher hinterließ ein toter
    Run am Item KEINE Spur (Spend-Limit-Abbruch 21.07.). Ohne Grund im Stempel: „reicht ja
    wenn ich sehe, dann kann ich reinschauen wieso"."""
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        bad = {"ok": False, "reply": "", "session_id": "", "denials": [], "context_tokens": 0,
               "usage_summary": {"cost_usd": 0.41}, "raw_error": "subtype=error_max_turns"}
        _t, _s, gl = gc_runner._outcome(bad, "aaaaaaaaaaaa", "T", sd)
        check("stempel: Fehllauf stempelt überhaupt", bool(gl))
        check("stempel: beginnt mit ❌", gl.startswith("❌ · "))
        check("stempel: nennt den Grund NICHT", "error_max_turns" not in gl and "subtype" not in gl)
        check("stempel: Kosten hängen wie beim Erfolg hinten", gl.endswith("$0.41"))
        # Formgleichheit ist kein Selbstzweck: board_kpis zählt „Runs heute" über das Datum,
        # das Frontend ersetzt den ERSTEN " · ". Beides muss weiter greifen.
        check("stempel: Datum bleibt parsebar (board_kpis 'Runs heute')",
              bool(server.DATE_ANY_RE.search(gl)))
        # _handoff_hint sucht "~Nk" — am ❌-Stempel darf es keinen Kontext-Hinweis geben.
        check("stempel: löst keinen Handoff-Hinweis aus", gc_runner._handoff_hint(gl) == "")
        good = {**bad, "ok": True, "context_tokens": 186000, "raw_error": ""}
        check("stempel: Erfolgsfall unverändert",
              gc_runner._outcome(good, "aaaaaaaaaaaa", "T", sd)[2].startswith("~186k · "))


def test_kopfzeile_zeigt_cache_tokens() -> None:
    """Warm-Cache-Runs sind billig, aber gross. Die CREW-Kopfzeile trug nur Kosten — ein
    Run mit 2 Mio gelesenen Cache-Tokens sah dort aus wie ein Run ohne Kontext (2026-08-25).
    Zwei Zusagen: `tok` zaehlt cache_read mit, und ein erfolgreicher Run stempelt auch
    dann, wenn der usage-Block keine Zahlen hergab."""
    warm = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "gc_id": "aaaaaaaaaaaa", "title": "Warm",
            "model": "opus", "ok": True, "input_tokens": 62, "cache_read": 2_620_021,
            "cache_creation": 109_053, "cost_usd": 1.23, "duration_ms": 1000}
    codex = {**warm, "gc_id": "bbbbbbbbbbbb", "title": "Codex", "model": "codex",
             "input_tokens": None, "cache_read": None, "cache_creation": None,
             "cost_usd": None, "duration_ms": None}
    orig = server._usage_tail
    server._usage_tail = lambda: [warm, codex]
    try:
        rows = {r["title"]: r for r in server.finished_recent()}
        check("kopfzeile: cache_read zaehlt in die Token-Zahl",
              rows["Warm"]["tok"] == 62 + 2_620_021 + 109_053)
        check("kopfzeile: Runs ohne Zahlen tragen None statt 0",
              rows["Codex"]["tok"] is None)
    finally:
        server._usage_tail = orig
    with tempfile.TemporaryDirectory() as td:
        leer = {"ok": True, "reply": "x", "session_id": "", "denials": [], "context_tokens": 0,
                "usage_summary": {"cost_usd": 0.09}, "raw_error": ""}
        gl = gc_runner._outcome(leer, "aaaaaaaaaaaa", "T", Path(td))[2]
        check("stempel: Erfolg ohne lesbare Tokens stempelt trotzdem", gl.startswith("~0k · "))
        check("stempel: und verliert dabei die Kosten nicht", gl.endswith("$0.09"))


def test_receipt_endpoint() -> None:
    """GET /api/gc-receipt — Gegenstück zu /api/gc-prompt. 404 statt 500, wenn ein Run
    älter ist als das Feature; die UI blendet den Menüeintrag dann als Fehler ein."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    base = f"http://127.0.0.1:{port}/api/gc-receipt"
    try:
        try:
            urllib.request.urlopen(f"{base}?id=ffffffffffff")
            check("receipt-endpoint: 404 ohne Receipt", False)
        except urllib.error.HTTPError as e:
            check("receipt-endpoint: 404 ohne Receipt", e.code == 404)
        try:
            urllib.request.urlopen(f"{base}?id=../../etc/passwd")
            check("receipt-endpoint: 400 bei bösartiger id", False)
        except urllib.error.HTTPError as e:
            check("receipt-endpoint: 400 bei bösartiger id", e.code == 400)
        # Mit Receipt: der Endpoint liefert den Text, den der Viewer rendert.
        receipt.RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        receipt.write("abcabcabcabc", "Endpoint-Test",
                      {"ok": True, "denials": [], "context_tokens": 1000, "session_id": "",
                       "raw_error": "", "usage_summary": {}}, "", time.time())
        j = json.load(urllib.request.urlopen(f"{base}?id=abcabcabcabc"))
        check("receipt-endpoint: liefert Receipt-Text", "Run receipt" in j["text"])
        check("receipt-endpoint: liefert Zeitstempel + Anzahl", bool(j["ts"]) and j["count"] >= 1)
    finally:
        httpd.shutdown()
        os.close(fd)
        os.unlink(tmp)


def test_guards_block_silent_loss() -> None:
    """Doppelte Meta-Zeile je Item → Guard >0 → Save wird geblockt statt still zu verlieren."""
    dbl_id = "## T\n\n### Jetzt\n\n- [ ] X *(2026-07-10)*\n  @gc-id: aaa\n  @gc-id: bbb\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n"
    check("guard: doppelte @gc-id → lost_id_lines>0", server.lost_id_lines(dbl_id, server.parse_board(dbl_id)) > 0)
    dbl_s = "## T\n\n### Jetzt\n\n- [ ] X *(2026-07-10)*\n  @gc-session: a\n  @gc-session: b\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n"
    check("guard: doppelte @gc-session → lost_session_lines>0", server.lost_session_lines(dbl_s, server.parse_board(dbl_s)) > 0)


def test_gc_pending_endpoint() -> None:
    """/api/gc-pending liefert genau die Items mit letztem Turn @gc: (for_gc)."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        r = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/gc-pending"))
        pend = r["pending"]
        titles = sorted(p["title"] for p in pend)
        check("endpoint: 2 pending (beide 'Offener Faden')", titles == ["Offener Faden", "Offener Faden"])
        check("endpoint: pending trägt id in addr", all(p["addr"]["id"] for p in pend))
        check("endpoint: pending trägt session", any(p["session"] for p in pend))
        rb = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
        check("endpoint: /api/board lost=0", rb["lost"] == 0)
        # Instance name comes from the folder, not from configuration.
        check("endpoint: /api/board traegt instance", rb["instance"] == server.GC_ROOT.name)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def _post(port: int, path: str, obj: dict) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def _post_raw(port: int, path: str, body: bytes, headers: dict) -> tuple[int, dict]:
    """Like `_post`, but with freely chosen headers — the CSRF guard test needs
    exactly the Origin/Content-Type combinations of the verified repro; `_post`
    hardcodes Content-Type to application/json."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_lost_guard_haelt_falsche_einrueckung() -> None:
    """Zeilen mit EINEM Leerzeichen oder Tab verschwanden still.

    Sie fielen aus allen Attribut-Zweigen UND aus dem Body-Zweig (der zwei
    Leerzeichen verlangt), und der lost-Guard zaehlt nur bekannte Zeilenfamilien
    — Freitext also nicht. Ergebnis: beim naechsten Save weg, ohne Warnung, und
    board_lint sah einen Verlust, den lost_total nicht sah. Failsafe-Prinzip:
    lieber falsch eingerueckt behalten als lautlos loeschen."""
    txt = ("## T\n\n### Jetzt\n\n- [ ] Item\n"
           "  saubere-zeile\n"
           " ein-leerzeichen\n"
           "\ttab-eingerueckt\n"
           "  @gc-id: aaaaaaaaaaaa\n\n# Notizen\n")
    board = server.parse_board(txt)
    body = board["themes"][0]["cols"]["Jetzt"][0]["body"]
    check("einrueckung: 1 Leerzeichen bleibt Body", "ein-leerzeichen" in body)
    check("einrueckung: Tab bleibt Body", "tab-eingerueckt" in body)
    out = server.serialize_board(board)
    check("einrueckung: ueberlebt den Round-Trip",
          "ein-leerzeichen" in out and "tab-eingerueckt" in out)
    check("einrueckung: lost_total sauber", server.lost_total(txt, board) == 0)
    check("einrueckung: lint stimmt mit lost_total ueberein",
          board_lint.lint(txt)["lost"] == 0)

    # Auch in der Personen-Sektion und im Cockpit (drei getrennte elif-Ketten —
    # genau die Stelle, an der man erfahrungsgemaess eine vergisst).
    persons = ("## T\n\n### Jetzt\n\n# Personen\n\n## A\n\n- [ ] P-Item\n"
               " schief-eingerueckt\n  @gc-id: bbbbbbbbbbbb\n\n# Notizen\n")
    pb = server.parse_board(persons)
    check("einrueckung: Personen-Sektion haelt die Zeile",
          "schief-eingerueckt" in pb["persons"][0]["items"][0]["body"])
    check("einrueckung: Personen lost_total sauber", server.lost_total(persons, pb) == 0)


def test_csrf_guard_blocks_cross_origin_writes() -> None:
    """Verified finding (2026-08-23, review): a POST with
    `Origin: https://attacker.example` + `Content-Type: text/plain` was accepted,
    created a card, and started a real agent run — any website open in a browser
    could trigger it (`text/plain` is a CORS "simple request", the browser sends it
    without a preflight; the server never checked Origin).

    The guard must close BOTH repro variants — text/plain AND application/json,
    since the foreign origin alone is already the finding — while leaving every
    local, non-browser caller (curl and every non-browser client) untouched: no
    Origin header at all is the normal case for those."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    payload = {"kind": "reply", "addr": {"id": "aaaaaaaaaaaa"}}
    try:
        # (a) exact verified repro: foreign origin, no-preflight Content-Type -> 403
        body = json.dumps({**payload, "text": "csrf via text-plain"}).encode()
        code, r = _post_raw(port, "/api/gc-append", body,
                            {"Origin": "https://attacker.example", "Content-Type": "text/plain"})
        check("csrf: text/plain + foreign origin -> 403", code == 403)
        check("csrf: 403 names a readable reason, not a stacktrace",
              set(r) == {"error"} and "cross" in r["error"])

        # (b) same foreign origin, but correct JSON — origin alone must be enough,
        # otherwise the content-type check would be the only line of defense
        body = json.dumps({**payload, "text": "csrf via json"}).encode()
        code, r = _post_raw(port, "/api/gc-append", body,
                            {"Origin": "https://attacker.example", "Content-Type": "application/json"})
        check("csrf: application/json + foreign origin -> still 403", code == 403)

        # (c) Sec-Fetch-Site: cross-site blocks even WITHOUT an Origin header
        body = json.dumps({**payload, "text": "csrf via sec-fetch-site"}).encode()
        code, r = _post_raw(port, "/api/gc-append", body, {"Content-Type": "application/json",
                                                            "Sec-Fetch-Site": "cross-site"})
        check("csrf: Sec-Fetch-Site: cross-site -> 403 even without Origin", code == 403)

        # (d) no Origin header — the normal path of every local CLI tool — still works
        body = json.dumps({**payload, "text": "local tool, no origin"}).encode()
        code, r = _post_raw(port, "/api/gc-append", body, {"Content-Type": "application/json"})
        check("csrf: no Origin header -> 200 (local tools stay unaffected)",
              code == 200 and r.get("ok"))

        # (e) Origin == this server itself (host+port) — the board UI — still works
        body = json.dumps({**payload, "text": "same origin"}).encode()
        code, r = _post_raw(port, "/api/gc-append", body,
                            {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
        check("csrf: own origin -> 200", code == 200 and r.get("ok"))

        # (f) GET stays unaffected by all of this, even with a foreign origin
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/gc-pending",
                                     headers={"Origin": "https://attacker.example"})
        with urllib.request.urlopen(req) as resp:
            check("csrf: GET unaffected, even with a foreign origin", resp.status == 200)

        # Only (d) and (e) may actually have written into the file.
        text = Path(tmp).read_text()
        check("csrf: text-plain repro wrote nothing", "csrf via text-plain" not in text)
        check("csrf: json cross-origin wrote nothing", "csrf via json" not in text)
        check("csrf: sec-fetch-site cross-site wrote nothing", "csrf via sec-fetch-site" not in text)
        check("csrf: no-origin call went through", "local tool, no origin" in text)
        check("csrf: own origin went through", "same origin" in text)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_gc_append_hardening() -> None:
    """SOL-Fixes: gc-append blockt bei lost-Guards (statt still zu vernichten),
    vergibt fehlende @gc-id beim Append, id kommt in der Response zurück."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        # Append per id auf Item A → 200, @gc-re landet in der Datei
        code, r = _post(port, "/api/gc-append",
                        {"kind": "reply", "text": "antwort vom agenten", "addr": {"id": "aaaaaaaaaaaa"}})
        check("append: per id → 200 + ok", code == 200 and r.get("ok") and r.get("id") == "aaaaaaaaaaaa")
        check("append: @gc-re in Datei", "@gc-re: antwort vom agenten" in Path(tmp).read_text())

        # Item OHNE id ('Beantwortet', per Fingerprint): Append vergibt eine id
        code, r = _post(port, "/api/gc-append",
                        {"kind": "ask", "text": "folgefrage",
                         "addr": {"scope": "theme", "name": "Thema", "col": "Jetzt",
                                  "title": "Beantwortet", "date": "2026-07-10"}})
        new_id = r.get("id", "")
        check("append: id-loses Item bekommt id (12 hex)", code == 200 and len(new_id) == 12
              and all(c in "0123456789abcdef" for c in new_id))
        check("append: @gc-id in Datei persistiert", f"@gc-id: {new_id}" in Path(tmp).read_text())
        # → damit liefert gc-pending jetzt NIE mehr id="" für dieses Item
        pend = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/gc-pending"))["pending"]
        check("append: gc-pending trägt überall id", pend and all(p["addr"]["id"] for p in pend))

        # Kaputtes ZIELITEM (zwei @wait-Zeilen, der Parser behält nur eine) → 409,
        # Datei bleibt byte-identisch. Andere Items sind davon nicht betroffen —
        # das prüft test_gc_append_chirurgisch.
        broken = SYNTH.replace("@gc-id: bbbbbbbbbbbb",
                               "@gc-id: bbbbbbbbbbbb\n  @wait: alex\n  @wait: maria")
        Path(tmp).write_text(broken)
        code, r = _post(port, "/api/gc-append",
                        {"kind": "reply", "text": "x", "addr": {"id": "bbbbbbbbbbbb"}})
        check("append: kaputtes Zielitem → 409", code == 409 and "unparsed" in r.get("error", ""))
        check("append: 409 benennt das Item", "Offener Faden" in r.get("error", ""))
        check("append: Datei nach 409 unverändert", Path(tmp).read_text() == broken)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_gc_append_sys_turn() -> None:
    """Radar-Melder (radar_watch.py, 19.08.): ein `sys`-Turn darf angehaengt werden,
    landet als @gc-sys: in der Datei — und kippt den Item-Status NICHT.

    Der Punkt der ganzen Uebung: Item A wartet auf den Agenten (letzter Turn @gc:).
    Haenge der Radar seine Meldung als `reply` an, waere die offene Aufgabe still als
    beantwortet markiert und aus der Arbeitsliste verschwunden. Mit `sys` bleibt sie
    stehen (thread_status filtert sys), und der Ungelesen-Punkt bleibt dem eigenen
    Gespraech des Nutzers vorbehalten."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        before = server.thread_status(server.find_item(
            server.parse_board(Path(tmp).read_text()), {"id": "aaaaaaaaaaaa"})[0])
        code, r = _post(port, "/api/gc-append",
                        {"kind": "sys", "text": "📡 Radar · !343 · blockiert, nicht haengend",
                         "addr": {"id": "aaaaaaaaaaaa"}})
        check("append sys: → 200", code == 200 and r.get("ok"))
        check("append sys: @gc-sys: in der Datei", "@gc-sys: 📡 Radar" in Path(tmp).read_text())
        board = server.parse_board(Path(tmp).read_text())
        it = server.find_item(board, {"id": "aaaaaaaaaaaa"})[0]
        check("append sys: Status unveraendert (kein for_owner)",
              server.thread_status(it) == before == "for_gc")
        check("append sys: Turn ist im Faden", it["thread"][-1]["kind"] == "sys")
        check("append sys: Round-Trip verlustfrei",
              server.parse_board(server.serialize_board(board)) == board)
        code, _ = _post(port, "/api/gc-append",
                        {"kind": "kaboom", "text": "x", "addr": {"id": "aaaaaaaaaaaa"}})
        check("append: unbekannter kind weiterhin 400", code == 400)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_gc_append_radar_ist_nativ_ausser_bei_offenem_auftrag() -> None:
    """Radar ist eine normale ungelesene Agentenantwort, darf aber nie einen bereits
    wartenden Nutzer-Turn als beantwortet markieren. Die Wahl passiert im Append-Lock."""
    source = """## Dev\n\n### Jetzt\n\n- [ ] Frei\n  @gc-id: aaaaaaaaaaaa\n  @gc-re: alter Stand\n\n- [ ] Wartet\n  @gc-id: bbbbbbbbbbbb\n  @gc: bitte noch erledigen\n\n### Bald\n\n### Geparkt\n"""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(source)
    httpd, port = _serve(Path(tmp))
    try:
        code, native = _post(port, "/api/gc-append", {
            "kind": "radar", "text": "📡 Radar · Review ist da",
            "addr": {"id": "aaaaaaaaaaaa"}})
        code2, context = _post(port, "/api/gc-append", {
            "kind": "radar", "text": "📡 Radar · CI ist rot",
            "addr": {"id": "bbbbbbbbbbbb"}})
        board = server.parse_board(Path(tmp).read_text())
        free = server.find_item(board, {"id": "aaaaaaaaaaaa"})[0]
        waiting = server.find_item(board, {"id": "bbbbbbbbbbbb"})[0]
        check("radar: freies Item speichert native Antwort",
              code == 200 and native.get("kind") == "reply"
              and free["thread"][-1]["kind"] == "reply"
              and server.thread_status(free) == "for_owner")
        check("radar: offener Auftrag bleibt offen",
              code2 == 200 and context.get("kind") == "sys"
              and waiting["thread"][-1]["kind"] == "sys"
              and server.thread_status(waiting) == "for_gc")
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_resume_prompt_traegt_externen_radar_turn_eine_runde_mit() -> None:
    pending = {"addr": {"id": "aaaaaaaaaaaa", "name": "Dev", "col": "Jetzt"},
               "title": "Radar-Kontext", "body": [], "session": "sess",
               "thread": [
                   {"kind": "ask", "text": "alte Frage"},
                   {"kind": "reply", "text": "alte Antwort"},
                   {"kind": "reply", "text": "📡 Radar · Review ist da"},
                   {"kind": "ask", "text": "und jetzt?"},
               ], "last_ask": "und jetzt?"}
    prompt = gc_runner.build_prompt(pending, resume=True)
    check("radar: Resume bekommt externen Turn", "📡 Radar · Review ist da" in prompt)
    check("radar: Resume markiert externe Herkunft", "outside this CLI session" in prompt)

    pending["thread"].extend([
        {"kind": "reply", "text": "neue Antwort"},
        {"kind": "ask", "text": "noch eine Runde"},
    ])
    pending["last_ask"] = "noch eine Runde"
    prompt2 = gc_runner.build_prompt(pending, resume=True)
    check("radar: Kontext altert nach einer Runde aus", "📡 Radar · Review ist da" not in prompt2)


def test_gc_body_endpoint() -> None:
    """Body/Stage ohne Hand-Splice: stale-safe, idempotent und item-lokal."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        original_etag = server.item_body_etag(["Body-Zeile"])
        code, result = _post(port, "/api/gc-body", {
            "addr": {"id": "aaaaaaaaaaaa"},
            "body": "Neue Kurzzeile\n\n···\n### Working state\nStatus: tested",
            "bodyEtag": original_etag,
        })
        check("gc-body: Body-Replace → 200", code == 200 and result.get("changed") is True)
        item = server.find_item(server.parse_board(Path(tmp).read_text()), {"id": "aaaaaaaaaaaa"})[0]
        check("gc-body: Leerzeilen normalisiert, Inhalt erhalten",
              item["body"] == ["Neue Kurzzeile", "···", "### Working state", "Status: tested"])
        fresh_etag = server.item_body_etag(item["body"])
        check("gc-body: Antwort trägt neue Body-Revision", result.get("bodyEtag") == fresh_etag)

        before_stale = Path(tmp).read_text()
        code, result = _post(port, "/api/gc-body", {
            "addr": {"id": "aaaaaaaaaaaa"}, "body": "Veralteter Stand",
            "bodyEtag": original_etag,
        })
        check("gc-body: stale Body → 409 + aktuelle Revision",
              code == 409 and result.get("bodyEtag") == fresh_etag)
        check("gc-body: stale 409 schreibt nichts", Path(tmp).read_text() == before_stale)

        stage = "strange-stage · bewusst unbekannt *(2026-08-22)*"
        code, result = _post(port, "/api/gc-body",
                             {"addr": {"id": "aaaaaaaaaaaa"}, "stage": stage})
        check("gc-body: unbekannte Stage bleibt erlaubt", code == 200 and result.get("changed") is True)
        code, result = _post(port, "/api/gc-body",
                             {"addr": {"id": "aaaaaaaaaaaa"}, "stage": stage})
        text = Path(tmp).read_text()
        check("gc-body: identischer Stage-Append ist idempotent",
              code == 200 and result.get("changed") is False and text.count(f"@stage: {stage}") == 1)

        before_injection = text
        code, _ = _post(port, "/api/gc-body", {
            "addr": {"id": "aaaaaaaaaaaa"}, "body": "@gc-id: injected-id",
            "bodyEtag": server.item_body_etag(
                server.find_item(server.parse_board(text), {"id": "aaaaaaaaaaaa"})[0]["body"]),
        })
        check("gc-body: Body-Metazeile wird abgelehnt", code == 400)
        check("gc-body: Injection-Fehler schreibt nichts", Path(tmp).read_text() == before_injection)
    finally:
        httpd.shutdown()
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)


def test_gc_body_parent() -> None:
    """Umhängen ohne Hand-Splice — dieselbe Ein-Ebenen-Regel wie gc-spawn-sub."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        code, result = _post(port, "/api/gc-body", {"addr": {"id": "bbbbbbbbbbbb"}, "parent": "aaaaaaaaaaaa"})
        text = Path(tmp).read_text()
        check("gc-body parent: Umhängen → 200 + @gc-parent-Zeile",
              code == 200 and result.get("changed") is True
              and "  @gc-id: bbbbbbbbbbbb\n  @gc-parent: aaaaaaaaaaaa\n" in text)
        code, _ = _post(port, "/api/gc-body", {"addr": {"id": "bbbbbbbbbbbb"}, "parent": "aaaaaaaaaaaa"})
        check("gc-body parent: idempotent", code == 200 and Path(tmp).read_text() == text)
        code, _ = _post(port, "/api/gc-body", {"addr": {"id": "aaaaaaaaaaaa"}, "parent": "bbbbbbbbbbbb"})
        check("gc-body parent: Sub eines Subs → 409", code == 409)
        code, _ = _post(port, "/api/gc-body", {"addr": {"id": "bbbbbbbbbbbb"}, "parent": "bbbbbbbbbbbb"})
        check("gc-body parent: Selbstbezug → 400", code == 400)
        code, _ = _post(port, "/api/gc-body", {"addr": {"id": "bbbbbbbbbbbb"}, "parent": "zzzzzzzzzzzz"})
        check("gc-body parent: unbekanntes Ziel → 404", code == 404)
        check("gc-body parent: Fehler schreiben nichts", Path(tmp).read_text() == text)
        code, result = _post(port, "/api/gc-body", {"addr": {"id": "bbbbbbbbbbbb"}, "parent": ""})
        check("gc-body parent: '' löst die Kante", code == 200 and result.get("changed") is True
              and "@gc-parent" not in Path(tmp).read_text())
        check("gc-body parent: Datei wieder wie vorher", Path(tmp).read_text() == SYNTH)
    finally:
        httpd.shutdown()
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)


def test_gc_body_chirurgisch_bei_nichtkanonischer_datei() -> None:
    """lost=0 reicht nicht: fremde, nur umsortierbare Zeilen bleiben byteidentisch."""
    # Item B: Body steht nach @gc-id. Nichts geht verloren, aber ein Full-Serialize
    # wuerde ihn vor die ID ziehen. Item A selbst bleibt kanonisch und beschreibbar.
    raw = SYNTH.replace(
        "  @gc-id: bbbbbbbbbbbb\n  @gc: zweite frage, gleicher titel+datum",
        "  @gc-id: bbbbbbbbbbbb\n  Fremde nichtkanonische Body-Zeile\n  @gc: zweite frage, gleicher titel+datum",
    )
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(raw)
    httpd, port = _serve(Path(tmp))
    try:
        board = server.parse_board(raw)
        check("gc-body chirurgisch: Fixture lost=0", server.lost_total(raw, board) == 0)
        check("gc-body chirurgisch: Fixture bewusst nicht byte-kanonisch",
              server.serialize_board(board) != raw)
        target = server.locate_item_block(raw, {"id": "aaaaaaaaaaaa"})
        assert target is not None
        start, end, _ = target
        old_lines = raw.split("\n")

        code, result = _post(port, "/api/gc-body", {
            "addr": {"id": "aaaaaaaaaaaa"},
            "stage": "review · item-local *(2026-08-22)*",
        })
        after = Path(tmp).read_text()
        new_target = server.locate_item_block(after, {"id": "aaaaaaaaaaaa"})
        assert new_target is not None
        new_start, new_end, _ = new_target
        new_lines = after.split("\n")
        check("gc-body chirurgisch: Write klappt", code == 200 and result.get("changed"))
        check("gc-body chirurgisch: Prefix ausserhalb Ziel byteidentisch",
              old_lines[:start] == new_lines[:new_start])
        check("gc-body chirurgisch: Suffix ausserhalb Ziel byteidentisch",
              old_lines[end:] == new_lines[new_end:])

        # Ist gerade das Zielitem nicht kanonisch, darf der Endpoint es nicht
        # nebenbei umsortieren — ehrliches 409 statt grossem Überraschungsdiff.
        target_bad = raw.replace(
            "  Body-Zeile\n  @gc-id: aaaaaaaaaaaa",
            "  @gc-id: aaaaaaaaaaaa\n  Body-Zeile",
        )
        Path(tmp).write_text(target_bad)
        code, _ = _post(port, "/api/gc-body", {
            "addr": {"id": "aaaaaaaaaaaa"}, "stage": "tested *(2026-08-22)*",
        })
        check("gc-body chirurgisch: nichtkanonisches Ziel → 409", code == 409)
        check("gc-body chirurgisch: Ziel-409 schreibt nichts", Path(tmp).read_text() == target_bad)
    finally:
        httpd.shutdown()
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)


def test_gc_append_chirurgisch() -> None:
    """Blast-Radius (Vorfall 28.07.): eine ungeparste Zeile an Item B darf die
    Antwort an Item A nicht mehr blocken — und der kaputte Block muss dabei
    Byte für Byte stehen bleiben, statt vom Reserialisieren verschluckt zu werden."""
    # Item B bekommt eine zweite @gc-id — parse_board behält nur die letzte,
    # serialize_board würde die erste still fressen. Genau die Klasse Defekt.
    broken = SYNTH.replace("@gc-id: bbbbbbbbbbbb", "@gc-id: bbbbbbbbbbbb\n  @gc-id: cccccccccccc")
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(broken)
    httpd, port = _serve(Path(tmp))
    try:
        board = server.parse_board(broken)
        check("chirurgisch: Fixture ist wirklich kaputt", server.lost_total(broken, board) > 0)

        code, r = _post(port, "/api/gc-append",
                        {"kind": "reply", "text": "antwort trotz defekt",
                         "addr": {"id": "aaaaaaaaaaaa"}, "session": "sess-neu"})
        after = Path(tmp).read_text()
        check("chirurgisch: gesundes Item bleibt beantwortbar", code == 200 and r.get("ok"))
        check("chirurgisch: Turn steht in der Datei", "@gc-re: antwort trotz defekt" in after)
        check("chirurgisch: Session-Pointer mitgeschrieben", "@gc-session: sess-neu" in after)
        check("chirurgisch: kaputte Zeile überlebt", "@gc-id: cccccccccccc" in after
              and "@gc-id: bbbbbbbbbbbb" in after)
        check("chirurgisch: Defekt bleibt sichtbar (kein stiller Heilungs-Effekt)",
              server.lost_total(after, server.parse_board(after)) > 0)

        # Alles außerhalb des Zielblocks ist byteidentisch — das ist die eigentliche
        # Zusage des Pfades, nicht nur "Datei kaputt-frei".
        vorher, nachher = broken.split("\n"), after.split("\n")
        s, e = next((s, e) for s, e in server.raw_item_blocks(broken)
                    if any("aaaaaaaaaaaa" in ln for ln in vorher[s:e]))
        check("chirurgisch: Zeilen vor dem Block unberührt", vorher[:s] == nachher[:s])
        check("chirurgisch: Zeilen nach dem Block unberührt",
              vorher[e:] == nachher[e + (len(nachher) - len(vorher)):])

        # Gegenprobe: derselbe Append auf gesundem Board muss dasselbe Ergebnis
        # liefern wie der Whole-Board-Weg — sonst driften die zwei Wege auseinander.
        Path(tmp).write_text(SYNTH)
        _post(port, "/api/gc-append",
              {"kind": "reply", "text": "antwort trotz defekt",
               "addr": {"id": "aaaaaaaaaaaa"}, "session": "sess-neu"})
        sauber = Path(tmp).read_text()
        chirurg = server.serialize_board(server.parse_board(after))
        check("chirurgisch: gleiches Ergebnis wie Whole-Board-Weg",
              [ln for ln in chirurg.split("\n") if "aaaaaaaaaaaa" in ln or "antwort trotz" in ln]
              == [ln for ln in sauber.split("\n") if "aaaaaaaaaaaa" in ln or "antwort trotz" in ln])
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_raw_item_blocks() -> None:
    """Blockgrenzen im Rohtext: Header-Checkboxen sind keine Items (die Falle vom
    28.07.), und jeder Block endet vor dem nächsten Item bzw. der nächsten Überschrift."""
    text = ("# Board\n- [ ] das hier ist HEADER-Text, kein Item\n\n"
            "## Thema\n\n### Jetzt\n\n"
            "- [ ] Erstes *(2026-08-07)*\n  @gc-id: aaaaaaaaaaaa\n  - [ ] Sub-Punkt\n\n"
            "- [ ] Zweites *(2026-08-07)*\n  @gc-id: bbbbbbbbbbbb\n\n"
            "# Notizen\n\n- [ ] auch das ist kein Item\n")
    lines = text.split("\n")
    blocks = server.raw_item_blocks(text)
    check("blocks: genau die zwei echten Items", len(blocks) == 2)
    check("blocks: Header-Checkbox ignoriert",
          all("HEADER-Text" not in lines[s] for s, _e in blocks))
    check("blocks: Notizen-Checkbox ignoriert",
          all("kein Item" not in lines[s] for s, _e in blocks))
    s, e = blocks[0]
    check("blocks: Sub-Punkt gehört zum Block", lines[s:e] ==
          ["- [ ] Erstes *(2026-08-07)*", "  @gc-id: aaaaaaaaaaaa", "  - [ ] Sub-Punkt"])
    check("blocks: Leerzeile gehört nicht dazu", lines[e].strip() == "")
    for s, e in blocks:
        it = server._parse_block(lines[s:e])
        check(f"blocks: Block {s} round-trippt", it is not None and server.item_lines(it) == lines[s:e])


def test_new_id_collision_retry() -> None:
    """_new_id retryt bei Kollision statt eine doppelte Run-Identität zu vergeben."""
    seq = iter(["aaaaaaaaaaaa" + "0" * 20, "bbbbbbbbbbbb" + "0" * 20])

    class FakeUUID:
        def __init__(self) -> None:
            self.hex = next(seq)

    orig = server.uuid.uuid4
    server.uuid.uuid4 = FakeUUID  # type: ignore[assignment]
    try:
        got = server._new_id({"aaaaaaaaaaaa"})
        check("_new_id: Kollision → nächster Versuch", got == "bbbbbbbbbbbb")
    finally:
        server.uuid.uuid4 = orig


# ---------------------------------------------------------------- runner

def _fake_claude(dirpath: Path, name: str, py_body: str) -> str:
    """Fake-claude-Binary (Python-Skript): testet den kompletten Runner-Pfad
    Spawn→JSON-Parse→Envelope→Post-back ohne echte API-Kosten."""
    p = dirpath / name
    p.write_text("#!/usr/bin/env python3\nimport json, sys, time\n" + py_body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


OK_JSON = ('print(json.dumps({"result": "testantwort vom agenten", "session_id": "fa4e5e55-0000-4000-8000-00000000e2e1",'
           ' "permission_denials": [], "subtype": "success", "is_error": False}))')


def test_runner_spawn_envelopes() -> None:
    """spawn_claude wirft NIE — jeder Fehlermodus wird zum Outcome-Dict."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        ok = gc_runner.spawn_claude("prompt", "", _fake_claude(d, "ok", OK_JSON), 10)
        check("spawn: success geparst", ok["ok"] and ok["reply"] == "testantwort vom agenten"
              and ok["session_id"] == "fa4e5e55-0000-4000-8000-00000000e2e1")

        err = gc_runner.spawn_claude("p", "", _fake_claude(
            d, "err", 'print(json.dumps({"result": "kaputt", "subtype": "error_during_execution",'
                      ' "is_error": True, "session_id": "s", "permission_denials": []}))'), 10)
        check("spawn: is_error → ok=False + subtype im raw_error", not err["ok"] and "error_during_execution" in err["raw_error"])

        garb = gc_runner.spawn_claude("p", "", _fake_claude(
            d, "garb", 'print("kein json"); sys.exit(3)'), 10)
        check("spawn: Garbage → ok=False + 'no result'", not garb["ok"] and "no result" in garb["raw_error"])

        # Ohne Journal gibt es keinen Ereignisstrom, also auch keinen Stillstandsbegriff —
        # dieser Pfad kennt nur die Notbremse (Umstellung 2026-07-27).
        slow = gc_runner.spawn_claude("p", "", _fake_claude(d, "slow", "time.sleep(30)"), 1)
        check("spawn: Notbremse → ok=False + Gesamtlaufzeit-Meldung", not slow["ok"] and "Safety stop" in slow["raw_error"])

        gone = gc_runner.spawn_claude("p", "", str(d / "gibts-nicht"), 5)
        check("spawn: Binary fehlt → ok=False", not gone["ok"] and "not found" in gone["raw_error"])

        # --resume erreicht das Binary als argv[1:3]
        res = gc_runner.spawn_claude("p", "resume-uuid-42", _fake_claude(
            d, "args", 'print(json.dumps({"result": " ".join(sys.argv[1:3]), "session_id": "s",'
                       ' "permission_denials": [], "subtype": "success", "is_error": False}))'), 10)
        check("spawn: --resume <uuid> an claude durchgereicht", res["reply"] == "--resume resume-uuid-42")

        board_url = "http://127.0.0.1:47901"
        env = gc_runner.spawn_claude("p", "", _fake_claude(
            d, "env", 'import os\nprint(json.dumps({"result": os.environ.get("GC_BOARD_URL"), '
            '"session_id": "s", "permission_denials": [], "subtype": "success", '
            '"is_error": False}))'), 10, extra_env={"GC_BOARD_URL": board_url})
        check("spawn: aktive Board-URL erreicht Claude", env["reply"] == board_url)


def test_spawn_ueberlebt_geloeschtes_journal() -> None:
    """Der letzte Rest des `[Errno 2]`-Crashes (23.07., dieser Faden: „schon wieder").

    v0.15.6 hinderte die Journal-Wache daran, LEBENDE Runs abzuräumen — geprüft über
    `_pid_alive(meta["pid"])`. Diese pid ist aber die des CLAUDE-KINDS, nicht die des
    Runners: sobald das Kind exitet, ist der Run für die Wache „tot", während
    `spawn_claude` die .out.json noch lesen will. Die pid-Bremse kann dieses Fenster
    prinzipiell nicht schließen, sie verkleinert es nur.

    Hier stirbt die Datei GARANTIERT im richtigen Moment: das Fake-Binary löscht die
    eigene stdout-Datei, nachdem es geschrieben hat. Über den Pfad gelesen → `[Errno 2]`
    und der ganze Run stirbt als „Runner-Crash"; aus dem offenen fd gelesen → egal.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        jdir = d / "journal"
        journal = gc_runner.RunJournal("ffffffffffff", "Journal weg", "http://x", 60, journal_dir=jdir)
        # schreibt die Antwort und reißt sich danach selbst die stdout-Datei weg
        killer = _fake_claude(d, "killer", "import os\n" + OK_JSON
                              + f"\nsys.stdout.flush()\nos.unlink({str(journal.out_path)!r})")
        out = gc_runner.spawn_claude("p", "", killer, 10, journal=journal)
        check("spawn: gelöschtes Journal killt den Run nicht mehr",
              out["ok"] and out["reply"] == "testantwort vom agenten")
        check("spawn: Datei ist wirklich weg gewesen", not journal.out_path.exists())


def test_runner_inline_and_sidecar() -> None:
    """@gc-re ist eine Markdown-Einzeile: kurz → inline, lang/mehrzeilig → Sidecar."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        check("inline: kurze Antwort bleibt inline", gc_runner._inline_reply("id1", "T", "alles klar", d) == "alles klar")
        long = "Erste Zeile mit dem Ergebnis.\n\nDetails:\n" + "x" * 600
        ref = gc_runner._inline_reply("id1", "T", long, d)
        sidecars = list(d.glob("id1-*.md"))
        check("sidecar: Datei geschrieben", len(sidecars) == 1 and "x" * 600 in sidecars[0].read_text())
        check("sidecar: inline = 1. Zeile + Verweis, einzeilig",
              "\n" not in ref and ref.startswith("Erste Zeile mit dem Ergebnis.") and "full reply:" in ref)
        # Überlange erste Zeile: Schnitt am letzten Satzende vor Zeichen 200 statt Hard-Cut mitten im Wort
        run_on = ("Der Fix ist gebaut, getestet und committet — das Board zeigt jetzt Kurzantworten. "
                  "Dieser zweite Satz ist bewusst so lang, dass die ganze Zeile die 200-Zeichen-Grenze "
                  "deutlich reisst und der alte Hard-Cut mitten im Wort gelandet waere.\n\nDetails.")
        ref2 = gc_runner._inline_reply("id2", "T", run_on, d)
        check("sidecar: Überlänge → Schnitt am Satzende",
              ref2.split(" … → full reply:")[0]
              == "Der Fix ist gebaut, getestet und committet — das Board zeigt jetzt Kurzantworten.")


def test_gc_run_endpoint() -> None:
    """Durchstich über den Server: POST /api/gc-run → Fake-Agent → @gc-re +
    @gc-session landen im Markdown; Doppel-Run und Nicht-for_gc → 409."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", "time.sleep(0.8)\n" + OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("run: 202 accepted", code == 202 and r.get("ok"))
            code2, r2 = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("run: Doppel-Run → 409", code2 == 409 and "already in progress" in r2.get("error", ""))
            rb = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
            check("run: /api/board zeigt running", rb.get("running") == ["bbbbbbbbbbbb"])

            deadline = time.time() + 15
            while time.time() < deadline and "fa4e5e55-0000-4000-8000-00000000e2e1" not in Path(tmp).read_text():
                time.sleep(0.2)
            text = Path(tmp).read_text()
            check("run: @gc-re im Markdown", "@gc-re: testantwort vom agenten" in text)
            check("run: @gc-session mit uuid + label", "@gc-session: fa4e5e55-0000-4000-8000-00000000e2e1 · board-offener-faden" in text)
            deadline = time.time() + 5
            while time.time() < deadline and json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/board")).get("running"):
                time.sleep(0.1)
            rb2 = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
            check("run: running wieder leer + lost=0", rb2.get("running") == [] and rb2["lost"] == 0)

            # Item ist jetzt for_owner → erneuter Run muss 409 sein
            code3, r3 = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("run: for_owner → 409", code3 == 409 and "not waiting" in r3.get("error", ""))
            code4, _ = _post(port, "/api/gc-run", {"id": "unbekannt99"})
            check("run: unbekannte id → 409", code4 == 409)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_drain_lehnt_run_ab() -> None:
    """Neustart-Drain: /api/gc-run startet NICHT und sagt auch, warum.

    Zwei Dinge auf einmal: der Drain blockt jeden Run-Start (produktiv richtig — ein
    Neustart mitten im Run killt die Antwort), und die Absage nannte pauschal „läuft
    bereits", was wie ein Doppel-Run aussieht. Der Test hält beides fest UND dokumentiert
    die Testfalle: fällt die Umbiegung von RESTART_LOCK oben je weg, hängt der halbe
    Run-Pfad wieder am maschinenglobalen /tmp/board-restart.lock.
    """
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "board-restart.lock"
        lock.mkdir()  # restart-server.sh nimmt ein Verzeichnis als Lock (mkdir = atomar)
        echt, server.RESTART_LOCK = server.RESTART_LOCK, lock
        # Fake-Binary auch hier: die Gegenprobe unten startet einen ECHTEN Run-Thread.
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("drain: Run wird abgelehnt (409)", code == 409)
            check("drain: Absage nennt den Neustart", "restart" in r.get("error", "").lower())
            check("drain: nichts hängt in RUNNING", "bbbbbbbbbbbb" not in server.RUNNING)
            server.RESTART_LOCK = echt  # ohne Lock startet derselbe Aufruf sofort
            code2, _ = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("drain: ohne Lock wieder 202", code2 == 202)
            deadline = time.time() + 15  # Run auslaufen lassen, sonst bleibt die id in RUNNING
            while time.time() < deadline and "bbbbbbbbbbbb" in server.RUNNING:
                time.sleep(0.1)
        finally:
            server.RESTART_LOCK = echt
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_gc_run_failure_visible() -> None:
    """Fail gracefully: kaputter Agent → ❌-Antwort IM FADEN, nie stumm."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "boom", 'print("BOOM kein json"); sys.exit(2)')
        httpd, port = _serve(Path(tmp))
        try:
            code, _ = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("fail: 202 accepted", code == 202)
            deadline = time.time() + 15
            while time.time() < deadline and "❌" not in Path(tmp).read_text():
                time.sleep(0.2)
            text = Path(tmp).read_text()
            check("fail: ❌-Envelope im Faden", "@gc-re: ❌ Agent run failed:" in text and "no result" in text)
            check("fail: Board weiter verlustfrei parsebar",
                  server.lost_total(text, server.parse_board(text)) == 0)
            # E2E für Q3 (2026-07-23): der Fehllauf muss jetzt auch AM ITEM sichtbar sein,
            # nicht nur im Faden — vorher stempelte er nichts. Echter Pfad durch run_item,
            # nicht nur _outcome als Unit.
            check("fail: ❌-Stempel steht als @gc-last am Item", "@gc-last: ❌ · " in text)
            check("fail: Stempel verrät den Grund nicht", "@gc-last: ❌ · 20" in text
                  and "no result" not in text.split("@gc-last: ❌ · ")[1].split("\n")[0])
            # ... und das Receipt liegt daneben, geschrieben vom Runner im selben Lauf.
            rc = receipt.receipt_files(receipt.RECEIPT_DIR, "bbbbbbbbbbbb")
            check("fail: Receipt existiert nach echtem run_item", len(rc) > 0)
            check("fail: Receipt nennt den Abbruchgrund", bool(rc) and "no result" in rc[-1].read_text())
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_sol_final_fixes() -> None:
    """Finale SOL-Review 2026-07-12: ETag=Text-Paar, Multiline-Normalisierung,
    gc-run addr-Fallback + timeout-Validierung, Session-Handle-Härtung."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    # Item OHNE @gc-id und mit EINDEUTIGEM Titel (Hand-Edit-Szenario) für den addr-Fallback
    Path(tmp).write_text(SYNTH.replace(
        "- [ ] Offener Faden *(2026-07-10)*\n  @gc-id: bbbbbbbbbbbb\n  @gc: zweite frage, gleicher titel+datum",
        "- [ ] Faden ohne ID *(2026-07-10)*\n  @gc: frage ohne id"))
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            # ETag konsistent: Board-Response-ETag == Hash GENAU des gelesenen Texts
            rb = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
            check("etag: /api/board etag == text_etag(Datei)", rb["etag"] == server.text_etag(Path(tmp).read_text()))

            # Multiline-Ask wird normalisiert statt beim nächsten Parse zu zerfallen
            code, _ = _post(port, "/api/gc-append",
                            {"kind": "ask", "text": "zeile eins\nzeile zwei\n\nzeile drei",
                             "addr": {"id": "aaaaaaaaaaaa"}})
            text = Path(tmp).read_text()
            check("multiline: normalisiert zu Einzeiler", code == 200
                  and "@gc: zeile eins · zeile zwei · zeile drei" in text)
            check("multiline: Board danach verlustfrei", server.lost_total(text, server.parse_board(text)) == 0)

            # timeout-Müll → 400 und KEIN hängender RUNNING-Eintrag
            code, r = _post(port, "/api/gc-run", {"id": "aaaaaaaaaaaa", "timeout": "x"})
            rb2 = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
            check("run: timeout-Müll → 400, RUNNING leer", code == 400 and rb2["running"] == [])

            # addr-Fallback: Item ohne @gc-id per Fingerprint starten → id wird vergeben
            code, r = _post(port, "/api/gc-run",
                            {"id": "", "addr": {"scope": "theme", "name": "Thema", "col": "Jetzt",
                                                "title": "Faden ohne ID", "date": "2026-07-10",
                                                "id": ""}})
            check("run: addr-Fallback → 202 + neue id", code == 202 and len(r.get("id", "")) == 12)
            check("run: vergebene id persistiert", f"@gc-id: {r['id']}" in Path(tmp).read_text())
            deadline = time.time() + 15
            while time.time() < deadline and json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/board")).get("running"):
                time.sleep(0.2)

            # Direkter API-Save auf kaputtem On-Disk-Zustand → 409 (nicht nur UI-locked)
            cur = Path(tmp).read_text()
            broken = cur.replace("@gc-id: aaaaaaaaaaaa", "@gc-id: aaaaaaaaaaaa\n  @gc-id: ffffffffffff")
            Path(tmp).write_text(broken)
            code, _ = _post(port, "/api/board",
                            {"board": server.parse_board(broken), "baseEtag": server.text_etag(broken)})
            check("save: kaputter On-Disk-Zustand → 409 auch für API-Clients", code == 409)
            check("save: Datei unangetastet", Path(tmp).read_text() == broken)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)

    # Session-Handle-Härtung (unit)
    check("session_uuid: UUID ok", gc_runner.session_uuid("554342b5-54d4-4d55-80de-502a2a4b7b99 · label")
          == "554342b5-54d4-4d55-80de-502a2a4b7b99")
    check("session_uuid: Müll → leer (frische Session statt CLI-Crash)",
          gc_runner.session_uuid("kaputt!! handle · x") == "")
    check("dead-session: 'No conversation found' → Fallback ok",
          gc_runner._looks_like_dead_session({"raw_error": "subtype=error", "reply": "No conversation found with session ID"}))
    check("dead-session: Timeout → KEIN Fallback (Arbeit evtl. schon passiert)",
          not gc_runner._looks_like_dead_session({"raw_error": "Timeout nach 15 min (Session ist evtl. resumebar)", "reply": ""}))


def test_gc_run_all_and_sidecar_route() -> None:
    """Run-all: beide ⏳GC-Items werden (max 2 parallel) abgearbeitet; Sidecar-Route
    liefert gc-threads-Dateien und blockt alles außer sicheren Dateinamen."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    # zwei for_gc-Items — das zweite OHNE @gc-id (Live-Bug des Owners: Altbestand
    # ohne ids wurde von Run-all still übersprungen → "0 gestartet")
    Path(tmp).write_text(SYNTH.replace("  @gc-id: bbbbbbbbbbbb\n", ""))
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", "time.sleep(0.4)\n" + OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/gc-run-all", {})
            new_ids = [i for i in r.get("queued", []) if i != "aaaaaaaaaaaa"]
            check("runall: 202 + beide queued (id-loses bekam id) + limit 2", code == 202
                  and len(r["queued"]) == 2 and "aaaaaaaaaaaa" in r["queued"]
                  and len(new_ids) == 1 and len(new_ids[0]) == 12 and r["limit"] == 2)
            check("runall: vergebene id persistiert", f"@gc-id: {new_ids[0]}" in Path(tmp).read_text())
            deadline = time.time() + 20
            # Jeder Run postet seine Antwort einzeln — auf BEIDE neuen Antworten warten.
            # (Flake 2026-07-16: vorher wurde nur bis zur ERSTEN neuen @gc-re gewartet,
            #  die Assertion darunter verlangte aber schon beide.)
            while time.time() < deadline and Path(tmp).read_text().count("@gc-re: testantwort vom agenten") < 2:
                time.sleep(0.2)
            text = Path(tmp).read_text()
            check("runall: beide Items beantwortet",
                  text.count("@gc-re: testantwort vom agenten") == 2)
            deadline = time.time() + 5
            while time.time() < deadline and json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/board")).get("running"):
                time.sleep(0.1)
            code2, r2 = _post(port, "/api/gc-run-all", {})
            check("runall: nichts mehr pending → 200 + leer", code2 == 200 and r2["queued"] == [])

            gcdir = Path(tmp).parent / "gc-threads"
            gcdir.mkdir(exist_ok=True)
            (gcdir / "test-sidecar-route.md").write_text("Inhalt der langen Antwort")
            try:
                body = urllib.request.urlopen(f"http://127.0.0.1:{port}/gc-threads/test-sidecar-route.md").read().decode()
                check("sidecar-route: liefert Inhalt", "Inhalt der langen Antwort" in body)
                try:
                    st = urllib.request.urlopen(f"http://127.0.0.1:{port}/gc-threads/%2e%2e%2fboard.md").status
                except urllib.error.HTTPError as e:
                    st = e.code
                check("sidecar-route: Traversal → 404", st == 404)
            finally:
                (gcdir / "test-sidecar-route.md").unlink(missing_ok=True)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


ARGV_ECHO = ('print(json.dumps({"result": " ".join(sys.argv[1:]), "session_id": "s",'
             ' "permission_denials": [], "subtype": "success", "is_error": False}))')


def test_model_choice() -> None:
    """Modellwahl: --model wird durchgereicht (leer = kein Flag), Whitelist am Endpoint."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        res = gc_runner.spawn_claude("p", "", _fake_claude(d, "argv", ARGV_ECHO), 10, model="opus")
        check("model: --model opus an claude durchgereicht", "--model opus" in res["reply"])
        res2 = gc_runner.spawn_claude("p", "", _fake_claude(d, "argv2", ARGV_ECHO), 10)
        check("model: leer → kein --model-Flag", "--model" not in res2["reply"])

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        code, r = _post(port, "/api/gc-run", {"id": "aaaaaaaaaaaa", "model": "gpt5"})
        check("model: unbekanntes Modell → 400 (Einzel-Run)", code == 400 and "model" in r.get("error", ""))
        code2, _ = _post(port, "/api/gc-run-all", {"model": "gpt5"})
        check("model: unbekanntes Modell → 400 (Run-all)", code2 == 400)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_long_run_policy() -> None:
    """Long is a server-owned, one-shot launch policy—not a sticky item preference."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    calls = []
    real_launch = server.launch_gc_run
    server.launch_gc_run = lambda pending, base, cmd, timeout, **kw: (
        calls.append((pending, timeout, kw)) or True)
    httpd, port = _serve(Path(tmp))
    try:
        code, r = _post(port, "/api/gc-run",
                        {"id": "aaaaaaaaaaaa", "model": "opus-multi", "run_mode": "long"})
        check("long-run: 202 + serverseitige 6h-Kappe",
              code == 202 and r.get("run_mode") == "long"
              and r.get("timeout") == gc_runner.LONG_TIMEOUT
              and calls[-1][1] == gc_runner.LONG_TIMEOUT
              and calls[-1][2].get("run_mode") == "long")
        code2, _ = _post(port, "/api/gc-run", {"id": "aaaaaaaaaaaa", "run_mode": "forever"})
        check("long-run: unbekannter Modus abgelehnt", code2 == 400)
        code3, _ = _post(port, "/api/gc-run", {"id": "aaaaaaaaaaaa", "timeout": 999999})
        check("long-run: Browser darf keine freien Sekunden setzen", code3 == 400)
        code4, _ = _post(port, "/api/gc-run-all", {"run_mode": "long"})
        check("long-run: run-all kann keine 6h-Welle starten", code4 == 400)
    finally:
        httpd.shutdown()
        server.launch_gc_run = real_launch
        Path(tmp).unlink(missing_ok=True)

    p = {"run_mode": "long", "addr": {"id": "abcdef123456"}}
    block = gc_runner._long_run_block(p)
    check("long-run: Prompt verlangt Checkpoints + Synthese-Reserve",
          "manifest.md" in block and "45 minutes" in block and "watchdogs" in block)
    check("long-run: Standardprompt bleibt unverändert", gc_runner._long_run_block({}) == "")
    wrapped = gc_runner.keep_awake_argv(["agent", "--go"], True)
    expected = sys.platform == "darwin" and Path("/usr/bin/caffeinate").exists()
    check("long-run: macOS hält nur den gewählten Kindprozess wach",
          (wrapped[0] == "/usr/bin/caffeinate") == expected)
    with tempfile.TemporaryFile(mode="w+") as stream:
        stream.write("INIT\n" + "x" * 200 + "\nRESULT")
        clipped = gc_runner.bounded_run_text(stream, limit=40)
    check("long-run: dicker Ergebnisstrom behält Kopf + Ende begrenzt",
          clipped.startswith("INIT") and clipped.endswith("RESULT") and len(clipped) <= 41)


def test_sweep_respects_open_threads() -> None:
    """sweep.py: lost_total-Guard + Personen-Items mit offenem Faden bleiben."""
    import sweep
    b = sweep.parse_board("## T\n\n### Jetzt\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n## Anna\n\n"
                          "- [x] Altes Thema *(2020-01-01)*\n  @gc: offene frage\n\n"
                          "- [x] Erledigt ohne Faden *(2020-01-01)*\n\n# Notizen\n")
    items = b["persons"][0]["items"]
    check("sweep: open_thread erkennt offenen Personen-Faden", sweep.open_thread(items[0]))
    check("sweep: done ohne Faden ist sweepbar", not sweep.open_thread(items[1]))


def test_sweep_stamps_missing_done_at() -> None:
    """Retentions-Deadlock (Board-Maintenance 04.09.): ein abgehaktes Item ohne
    @done-at UND ohne *(Datum)* liefert done_at() == None, ripe() bleibt fuer immer
    False — die Karte stand seit Ende Juli abgehakt in „Team & Org / Jetzt". Der Sweep
    stempelt jetzt @done-at=jetzt; nichts verschwindet sofort, die 25h-Frist beginnt."""
    import sweep
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Ohne jeden Stempel\n\n"
           "- [x] Nur mit Datum *(2020-01-01)*\n\n"
           "- [ ] Offen ohne Stempel\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    b = sweep.parse_board(txt)
    items = b["themes"][0]["cols"]["Jetzt"]
    check("done-at-Deadlock: ohne beide Zeitquellen ist done_at() None",
          sweep.done_at(items[0]) is None)
    dated = sweep.stamp_missing_done_at(b)
    check("done-at-Deadlock: genau das stempellose Item wird gestempelt",
          len(dated) == 1 and bool(items[0]["done_at"])
          and not items[1].get("done_at") and not items[2].get("done_at"))
    check("done-at-Deadlock: nach dem Stempel ist die Karte ueberhaupt datierbar",
          sweep.done_at(items[0]) is not None)
    check("done-at-Deadlock: frisch gestempelt heisst noch NICHT reif (25h-Schonfrist)",
          "Ohne jeden Stempel" in sweep.serialize_board(b))


def test_sweep_closes_done_threads() -> None:
    """Archiv-Deadlock (2026-07-16, Q3): abgehaktes Item mit offenem Faden → Sweep
    hängt selbst @gc-done an (VOR der Archiv-Prüfung), reifes Item wird im SELBEN Lauf
    archiviert; frisches bleibt (Faden aber zu), offenes Item bleibt unberührt."""
    import sys

    import sweep
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Reif mit offenem Faden *(2020-01-01)*\n  @gc: offene frage\n\n"
           "- [x] Frisch mit offenem Faden *(2099-01-01)*\n  @gc: offene frage\n\n"
           "- [ ] Offen mit Faden *(2020-01-01)*\n  @gc: offene frage\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    b = sweep.parse_board(txt)
    closed = sweep.close_done_threads(b)
    items = b["themes"][0]["cols"]["Jetzt"]
    check("deadlock: beide abgehakten Fäden geschlossen", len(closed) == 2
          and not sweep.open_thread(items[0]) and not sweep.open_thread(items[1]))
    check("deadlock: nicht abgehaktes Item bleibt offen", sweep.open_thread(items[2]))
    check("deadlock: @gc-done trägt Auto-Vermerk",
          items[0]["thread"][-1] == {"kind": "done", "text": sweep.AUTO_DONE_NOTE})
    # Voller main()-Lauf gegen Temp-Kopien (NIE Live-board.md): Deadlock-Item raus.
    with tempfile.TemporaryDirectory() as td:
        board_f = Path(td) / "board.md"
        board_f.write_text(txt)
        arch_f = Path(td) / "board-archive.md"
        old = sweep.BOARD, sweep.ARCHIVE, sys.argv
        try:
            sweep.BOARD, sweep.ARCHIVE, sys.argv = board_f, arch_f, ["sweep.py"]
            rc = sweep.main()
        finally:
            sweep.BOARD, sweep.ARCHIVE, sys.argv = old
        after = board_f.read_text()
        check("deadlock: main() archiviert reifes Item mit Auto-@gc-done", rc == 0
              and "Reif mit offenem Faden" not in after and arch_f.exists()
              and "Reif mit offenem Faden" in arch_f.read_text()
              and f"@gc-done: {sweep.AUTO_DONE_NOTE}" in arch_f.read_text())
        check("deadlock: frisches Item bleibt im Board, Faden aber geschlossen",
              "Frisch mit offenem Faden" in after
              and f"@gc-done: {sweep.AUTO_DONE_NOTE}" in after)
        check("deadlock: offenes Item unberührt (kein Auto-Close)",
              after.count(f"@gc-done: {sweep.AUTO_DONE_NOTE}") == 1)


def test_sweep_retires_chat_cards() -> None:
    """Fuenfter Sweep-Job (2026-08-25): Cockpit-Tages-Chat-Karten legen sich selbst still.

    Vier Zusicherungen: (1) ruhende Chat-Karte wird abgehakt UND im selben Lauf archiviert
    - die 25h-Schonfrist gilt fuer sie NICHT; (2) eine Karte, in der gerade noch gearbeitet
    wurde, bleibt; (3) Cockpit-AKTIONS-Karten werden nie angefasst, auch nicht abgehakte -
    sie sind Dauer-Items; (4) Erkennung haengt am `chat:`-Marker, nicht am Titel."""
    import sys
    from datetime import datetime, timedelta

    import sweep
    fresh = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    stale = (datetime.now() - timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    txt = ("## T\n\n### Jetzt\n\n### Bald\n\n### Geparkt\n\n"
           "# Cockpit\n\n"
           f"- [ ] Chat 2026-08-23 *(2026-08-23)*\n  chat:2026-08-23\n  @gc-id: aaaaaaaaaaaa\n"
           f"  @gc: hallo\n  @gc-re: hi\n  @gc-last: ~67k · {stale} · $0.71\n\n"
           f"- [ ] Chat 2026-08-25 *(2026-08-25)*\n  chat:2026-08-25\n  @gc-id: bbbbbbbbbbbb\n"
           f"  @gc: laeuft noch\n  @gc-last: ~12k · {fresh} · $0.10\n\n"
           "- [x] Board-Maintenance *(2020-01-01)*\n  action:board-maintenance\n"
           "  @gc-id: cccccccccccc\n\n"
           "# Personen\n\n# Notizen\n")
    b = sweep.parse_board(txt)
    cock = b["cockpit"]
    check("chat-gc: Marker erkennt Chat-Karte, nicht die Aktions-Karte",
          sweep.is_chat_card(cock[0]) and not sweep.is_chat_card(cock[2]))
    retired = sweep.retire_chat_cards(b)
    check("chat-gc: nur die ruhende Karte wird abgehakt",
          len(retired) == 1 and cock[0]["done"] and not cock[1]["done"])
    # done_at ist UTC (wie überall im Board), `stale` ist Ortszeit — zwischen 09:00 und
    # 11:00 MESZ liegt now−9h auf einem anderen UTC-Datum. Also erst konvertieren.
    from datetime import timezone as _tz
    stale_utc_day = (datetime.strptime(stale, "%Y-%m-%d %H:%M")
                     .astimezone(_tz.utc).strftime("%Y-%m-%d"))
    check("chat-gc: done_at trägt den Ruhe-Zeitpunkt, nicht jetzt",
          cock[0]["done_at"].startswith(stale_utc_day))
    with tempfile.TemporaryDirectory() as td:
        board_f = Path(td) / "board.md"
        board_f.write_text(txt)
        arch_f = Path(td) / "board-archive.md"
        old = sweep.BOARD, sweep.ARCHIVE, sys.argv
        try:
            sweep.BOARD, sweep.ARCHIVE, sys.argv = board_f, arch_f, ["sweep.py"]
            rc = sweep.main()
        finally:
            sweep.BOARD, sweep.ARCHIVE, sys.argv = old
        after = board_f.read_text()
        check("chat-gc: main() archiviert die ruhende Karte im selben Lauf (keine 25h)",
              rc == 0 and "Chat 2026-08-23" not in after
              and "Chat 2026-08-23" in arch_f.read_text())
        check("chat-gc: laufende Chat-Karte bleibt im Board", "Chat 2026-08-25" in after)
        check("chat-gc: abgehakte Cockpit-AKTION wird nie archiviert",
              "Board-Maintenance" in after and "Board-Maintenance" not in arch_f.read_text())


def test_sweep_unknown_thread_kind() -> None:
    """Regression zum Ausfall 2026-07-27…30: sweep.py hielt eine eigene Tag-Map ohne den
    Kind „sys" — fmt_item warf KeyError im Sammel-Loop und riss den GESAMTEN Sweep ab, vier
    Nächte lang wurde nichts archiviert (board.md 250 KB gegen ~113 KB Baseline).

    Zwei Zusicherungen: (1) ein @gc-sys:-Turn wandert korrekt mit ins Archiv; (2) ein
    UNBEKANNTER Kind darf den Lauf nie mehr killen — er wird generisch serialisiert und
    gemeldet. (2) ist der eigentliche Defekt: der fehlende Map-Eintrag war nur sein Anlass."""
    import sys

    import sweep
    check("sweep: Tag-Map ist server.GC_TAG, keine zweite Kopie",
          sweep.GC_TAG is server.GC_TAG and sweep.GC_TAG["sys"] == "@gc-sys:")
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Reif mit sys-Turn *(2020-01-01)*\n  @gc-id: aaaabbbbcccc\n"
           "  @gc: frage\n  @gc-re: antwort\n  @gc-sys: ✓ Sub erledigt: Sub A\n"
           "  @gc-done: zu\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    with tempfile.TemporaryDirectory() as td:
        board_f, arch_f = Path(td) / "board.md", Path(td) / "board-archive.md"
        board_f.write_text(txt)
        old = sweep.BOARD, sweep.ARCHIVE, sys.argv
        try:
            sweep.BOARD, sweep.ARCHIVE, sys.argv = board_f, arch_f, ["sweep.py"]
            rc = sweep.main()
        finally:
            sweep.BOARD, sweep.ARCHIVE, sys.argv = old
        check("sweep: Item mit @gc-sys: wird archiviert statt KeyError", rc == 0
              and "Reif mit sys-Turn" not in board_f.read_text()
              and arch_f.exists() and "Reif mit sys-Turn" in arch_f.read_text())
        check("sweep: sys-Turn steht unverändert im Archiv",
              "@gc-sys: ✓ Sub erledigt: Sub A" in arch_f.read_text())
    # Unbekannter Kind: fmt_item darf NICHT werfen, sondern fällt auf @gc-<kind>: zurück.
    sweep.UNKNOWN_KINDS.clear()
    try:
        line = sweep.fmt_item(
            {"title": "X", "date": "2020-01-01", "thread": [{"kind": "kaboom", "text": "t"}]},
            "T / Jetzt")
        check("sweep: unbekannter Kind crasht nicht, wird generisch getaggt",
              "@gc-kaboom: t" in line and sweep.UNKNOWN_KINDS == {"kaboom"})
    except KeyError:
        check("sweep: unbekannter Kind crasht nicht, wird generisch getaggt", False)
    finally:
        sweep.UNKNOWN_KINDS.clear()


def test_sweep_heartbeat() -> None:
    """Heartbeat (2026-07-30, Faden addc8a2f375e): der Sweep stempelt nach JEDEM sauberen
    Lauf, damit `context-health-check.py` Guard 15 „lief die Müllabfuhr?" direkt beantworten
    kann statt über den Größen-Proxy zu raten.

    Die drei Fälle, auf die es ankommt: „nichts zu tun" ist GESUND und muss stempeln (sonst
    sähe ein ruhiges Board aus wie ein toter Sweep), ein Dry-Run darf NICHT stempeln (sonst
    hielte ein Trockenlauf den Guard künstlich grün), und ein nicht schreibbarer Heartbeat
    darf den Sweep nicht kippen — genau die Klasse Fehler, die den Ausfall verursacht hat."""
    import sys

    import sweep
    with tempfile.TemporaryDirectory() as td:
        board_f, arch_f = Path(td) / "board.md", Path(td) / "board-archive.md"
        hb = Path(td) / "journal" / "sweep-heartbeat.json"
        leer = "## T\n\n### Jetzt\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n"
        board_f.write_text(leer)
        old = sweep.BOARD, sweep.ARCHIVE, sweep.HEARTBEAT, sys.argv
        try:
            sweep.BOARD, sweep.ARCHIVE, sweep.HEARTBEAT = board_f, arch_f, hb
            sys.argv = ["sweep.py", "--dry-run"]
            sweep.main()
            check("heartbeat: Dry-Run stempelt NICHT", not hb.exists())
            sys.argv = ["sweep.py"]
            rc = sweep.main()
            check("heartbeat: „nichts zu tun\" ist ein gesunder Lauf und stempelt",
                  rc == 0 and hb.exists() and json.loads(hb.read_text())["swept"] == 0)
            # Echter Sweep mit Arbeit: Zähler landen im Stempel.
            board_f.write_text("## T\n\n### Jetzt\n\n- [x] Alt *(2020-01-01)*\n\n"
                               "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
            hb.unlink()
            sweep.main()
            check("heartbeat: Zähler des Laufs stehen im Stempel",
                  json.loads(hb.read_text())["swept"] == 1)
            # Unbeschreibbarer Pfad: Sweep muss trotzdem Exit 0 liefern.
            board_f.write_text(leer)
            sweep.HEARTBEAT = Path(td) / "journal" / "sweep-heartbeat.json" / "nope.json"
            check("heartbeat: kaputter Heartbeat kippt den Sweep nicht", sweep.main() == 0)
        finally:
            sweep.BOARD, sweep.ARCHIVE, sweep.HEARTBEAT, sys.argv = old


def test_sweep_sidecar_archive() -> None:
    """Faden-Retention (2026-07-21): wandert ein Item ins Archiv, wandern seine
    Sidecar-Dateien mit nach gc-threads/archive/ (flach), und die Verweise im archivierten
    Textblock zeigen auf den neuen Pfad. Sidecars eines NICHT archivierten Items (hier:
    noch offen, gar nicht abgehakt) bleiben in gc-threads/ liegen — nur Sidecars neu
    archivierter Items wandern."""
    import sys

    import sweep
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Mit Sidecars *(2020-01-01)*\n"
           "  @gc-id: aaaaaaaaaaaa\n"
           "  @done-at: 2020-01-01T00:00:00+00:00\n"
           "  @gc: frage … → voller Text: inbox/gc-threads/aaaaaaaaaaaa-20260101-000000-a1b2.md\n"
           "  @gc-re: antwort … → volle Antwort: inbox/gc-threads/aaaaaaaaaaaa-20260101-000001-c3d4.md\n"
           "  @gc-done: erledigt\n"
           "  @gc-session: 33333333-3333-3333-3333-333333333333 · board-mit-sidecars\n"
           "  @gc-sessions: 11111111-1111-1111-1111-111111111111, 22222222-2222-2222-2222-222222222222\n\n"
           "- [ ] Ohne Archivierung *(2026-07-20)*\n"
           "  @gc-id: bbbbbbbbbbbb\n"
           "  @gc: frage … → voller Text: inbox/gc-threads/bbbbbbbbbbbb-20260101-000000-e5f6.md\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        gcdir = root / "gc-threads"
        gcdir.mkdir()
        (gcdir / "aaaaaaaaaaaa-20260101-000000-a1b2.md").write_text("VOLLTEXT FRAGE")
        (gcdir / "aaaaaaaaaaaa-20260101-000001-c3d4.md").write_text("VOLLTEXT ANTWORT")
        (gcdir / "bbbbbbbbbbbb-20260101-000000-e5f6.md").write_text("FREMDER SIDECAR")
        board_f = root / "board.md"
        board_f.write_text(txt)
        arch_f = root / "board-archive.md"
        old = (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR, sys.argv)
        try:
            sweep.BOARD, sweep.ARCHIVE = board_f, arch_f
            sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR = gcdir, gcdir / "archive"
            sys.argv = ["sweep.py"]
            rc = sweep.main()
        finally:
            (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR,
             sweep.SIDECAR_ARCHIVE_DIR, sys.argv) = old
        after = board_f.read_text()
        arch_text = arch_f.read_text()
        check("sidecar-archive: rc 0", rc == 0)
        check("sidecar-archive: Item verschwindet aus board.md", "Mit Sidecars" not in after)
        check("sidecar-archive: beide Sidecars liegen jetzt in gc-threads/archive/",
              (gcdir / "archive" / "aaaaaaaaaaaa-20260101-000000-a1b2.md").exists()
              and (gcdir / "archive" / "aaaaaaaaaaaa-20260101-000001-c3d4.md").exists()
              and not (gcdir / "aaaaaaaaaaaa-20260101-000000-a1b2.md").exists()
              and not (gcdir / "aaaaaaaaaaaa-20260101-000001-c3d4.md").exists())
        check("sidecar-archive: Verweise im Archiv-Text zeigen auf neuen Pfad",
              "inbox/gc-threads/archive/aaaaaaaaaaaa-20260101-000000-a1b2.md" in arch_text
              and "inbox/gc-threads/archive/aaaaaaaaaaaa-20260101-000001-c3d4.md" in arch_text)
        check("sidecar-archive: Sidecar des NICHT archivierten Items bleibt unberührt",
              (gcdir / "bbbbbbbbbbbb-20260101-000000-e5f6.md").exists()
              and not (gcdir / "archive" / "bbbbbbbbbbbb-20260101-000000-e5f6.md").exists())
        check("sidecar-archive: @gc-session/@gc-sessions überleben fmt_item ins Archiv",
              "@gc-session: 33333333-3333-3333-3333-333333333333 · board-mit-sidecars" in arch_text
              and "@gc-sessions: 11111111-1111-1111-1111-111111111111, "
                  "22222222-2222-2222-2222-222222222222" in arch_text)


def test_sweep_sidecar_collision_warns() -> None:
    """Kollision (Zieldatei liegt schon in gc-threads/archive/) → Original bleibt liegen,
    NIE überschrieben — UND die Warnung ist sichtbar in der Sweep-Ausgabe (Mangel 1 im
    Review: sidecar_warnings wurde gesammelt, aber nie ausgegeben — stiller Fehlerpfad)."""
    import contextlib
    import io
    import sys

    import sweep
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Kollidiert *(2020-01-01)*\n"
           "  @gc-id: cccccccccccc\n"
           "  @done-at: 2020-01-01T00:00:00+00:00\n"
           "  @gc: frage … → voller Text: inbox/gc-threads/cccccccccccc-20260101-000000-x1.md\n"
           "  @gc-done: erledigt\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        gcdir = root / "gc-threads"
        gcdir.mkdir()
        src = gcdir / "cccccccccccc-20260101-000000-x1.md"
        src.write_text("ORIGINAL")
        arch_dir = gcdir / "archive"
        arch_dir.mkdir()
        collide = arch_dir / "cccccccccccc-20260101-000000-x1.md"
        collide.write_text("SCHON DA")
        board_f = root / "board.md"
        board_f.write_text(txt)
        arch_f = root / "board-archive.md"
        old = (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR, sys.argv)
        buf = io.StringIO()
        try:
            sweep.BOARD, sweep.ARCHIVE = board_f, arch_f
            sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR = gcdir, arch_dir
            sys.argv = ["sweep.py"]
            with contextlib.redirect_stdout(buf):
                rc = sweep.main()
        finally:
            (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR,
             sweep.SIDECAR_ARCHIVE_DIR, sys.argv) = old
        out = buf.getvalue()
        check("collision: rc 0 (Kollision ist Warnung, kein Fehler)", rc == 0)
        check("collision: Original bleibt in gc-threads/ liegen, unverändert",
              src.exists() and src.read_text() == "ORIGINAL")
        check("collision: Zieldatei bleibt unverändert (nicht überschrieben)",
              collide.read_text() == "SCHON DA")
        check("collision: Warnung ist sichtbar in der Sweep-Ausgabe",
              "Sidecar-Kollision" in out and "cccccccccccc-20260101-000000-x1.md" in out)


def test_sweep_sidecar_dry_run() -> None:
    """--dry-run bewegt keine Sidecar-Datei — board.md bleibt ebenfalls unverändert."""
    import sys

    import sweep
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Dry Run Item *(2020-01-01)*\n"
           "  @gc-id: dddddddddddd\n"
           "  @done-at: 2020-01-01T00:00:00+00:00\n"
           "  @gc: frage … → voller Text: inbox/gc-threads/dddddddddddd-20260101-000000-y1.md\n"
           "  @gc-done: erledigt\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        gcdir = root / "gc-threads"
        gcdir.mkdir()
        src = gcdir / "dddddddddddd-20260101-000000-y1.md"
        src.write_text("BLEIBT LIEGEN")
        board_f = root / "board.md"
        board_f.write_text(txt)
        arch_f = root / "board-archive.md"
        old = (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR, sys.argv)
        try:
            sweep.BOARD, sweep.ARCHIVE = board_f, arch_f
            sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR = gcdir, gcdir / "archive"
            sys.argv = ["sweep.py", "--dry-run"]
            rc = sweep.main()
        finally:
            (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR,
             sweep.SIDECAR_ARCHIVE_DIR, sys.argv) = old
        check("dry-run: rc 0", rc == 0)
        check("dry-run: board.md unverändert", board_f.read_text() == txt)
        check("dry-run: kein Archiv angelegt", not arch_f.exists())
        check("dry-run: Sidecar bleibt liegen, unverändert",
              src.exists() and src.read_text() == "BLEIBT LIEGEN")
        check("dry-run: kein archive/-Ordner angelegt", not (gcdir / "archive").exists())


def test_sweep_sidecar_order_safety() -> None:
    """Reihenfolge-Fix (Mangel 2 im Review): archive_item() sammelt während der Schleife nur
    den Move-PLAN — die eigentlichen shutil.move-Aufrufe laufen erst NACH dem erfolgreichen
    board.md-Write. Erzwingt hier künstlich einen Fehler beim Board-Schreiben (serialize_board
    wirft) und prüft: die Sidecar-Datei wurde NICHT verschoben, bevor board.md geschrieben war
    — vorher (Mangel) wäre sie zu diesem Zeitpunkt schon weg, aber board.md stünde noch mit
    dem alten Verweis da."""
    import sys

    import sweep
    txt = ("## T\n\n### Jetzt\n\n"
           "- [x] Crash Item *(2020-01-01)*\n"
           "  @gc-id: eeeeeeeeeeee\n"
           "  @done-at: 2020-01-01T00:00:00+00:00\n"
           "  @gc: frage … → voller Text: inbox/gc-threads/eeeeeeeeeeee-20260101-000000-z1.md\n"
           "  @gc-done: erledigt\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        gcdir = root / "gc-threads"
        gcdir.mkdir()
        src = gcdir / "eeeeeeeeeeee-20260101-000000-z1.md"
        src.write_text("DARF NICHT VERSCHWINDEN")
        board_f = root / "board.md"
        board_f.write_text(txt)
        arch_f = root / "board-archive.md"
        old = (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR,
               sweep.serialize_board, sys.argv)

        def boom(board: dict) -> str:
            raise RuntimeError("simulierter Schreibfehler")

        raised = False
        try:
            sweep.BOARD, sweep.ARCHIVE = board_f, arch_f
            sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR = gcdir, gcdir / "archive"
            sweep.serialize_board = boom
            sys.argv = ["sweep.py"]
            try:
                sweep.main()
            except RuntimeError:
                raised = True
        finally:
            (sweep.BOARD, sweep.ARCHIVE, sweep.SIDECAR_DIR, sweep.SIDECAR_ARCHIVE_DIR,
             sweep.serialize_board, sys.argv) = old
        check("order-safety: simulierter Schreibfehler wirft wie erwartet", raised)
        check("order-safety: Sidecar wurde NICHT verschoben, bevor board.md geschrieben war",
              src.exists() and src.read_text() == "DARF NICHT VERSCHWINDEN"
              and not (gcdir / "archive").exists())


def test_wait_field() -> None:
    """@wait: überlebt den Round-Trip (Feld + Bestätigungsdatum) und ist lost-Guard-geschützt."""
    txt = ("## Dev\n\n### Now\n\n### Waiting on others\n\n"
           "- [ ] MR offen *(2026-07-10)*\n  @gc-id: cccccccccccc\n  @wait: alex · !475 *(2026-07-12)*\n\n"
           "### Next\n\n### Backlog\n\n# To discuss\n\n# Notes\n")
    b = server.parse_board(txt)
    it = b["themes"][0]["cols"]["Wartet auf andere"][0]
    check("wait: Feld + Datum geparst", it["wait"] == "alex · !475" and it["wait_since"] == "2026-07-12")
    check("wait: Round-Trip verlustfrei", server.serialize_board(b).strip() == txt.strip())
    check("wait: lost-Guard sauber", server.lost_total(txt, b) == 0)
    # zweite @wait-Zeile am selben Item → Parser nimmt nur eine → Guard muss Save blocken
    dup = txt.replace("  @wait: alex · !475 *(2026-07-12)*\n",
                      "  @wait: alex · !475 *(2026-07-12)*\n  @wait: zweite zeile\n")
    check("wait: doppeltes Feld blockt Save", server.lost_total(dup, server.parse_board(dup)) > 0)


def test_wait_decay() -> None:
    """Wait-Eskalation (sweep, umgebaut 2026-07-22): unbestätigt > WAIT_DECAY_DAYS bleibt in
    „Wartet auf andere" und wandert nur nach OBEN — Referenz + Datum bleiben erhalten (sie
    tragen den Nachfass-Impuls). Frisches bleibt unten, Undatiertes wird heute gestempelt."""
    import sweep
    from datetime import date, timedelta
    today = date(2026, 7, 14)
    old = (today - timedelta(days=sweep.WAIT_DECAY_DAYS + 1)).isoformat()
    fresh = (today - timedelta(days=1)).isoformat()
    b = sweep.parse_board(
        "## Dev\n\n### Jetzt\n\n### Wartet auf andere\n\n"
        f"- [ ] Frisch *(2026-07-01)*\n  @wait: hermes · !12 *({fresh})*\n\n"
        f"- [ ] Alt *(2026-07-01)*\n  @wait: alex · !475 *({old})*\n\n"
        "- [ ] Undatiert *(2026-07-01)*\n  @wait: irgendwer\n\n"
        "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    overdue, stamped, changed = sweep.escalate_waits(b, today)
    cols = b["themes"][0]["cols"]
    titles_wait = [i["title"] for i in cols["Wartet auf andere"]]
    check("eskalation: nichts landet in „Jetzt“", cols["Jetzt"] == [])
    check("eskalation: Überfälliges nach oben, Rest stabil",
          titles_wait == ["Alt", "Frisch", "Undatiert"] and changed)
    check("eskalation: überfälliges Wait behält Referenz + Datum",
          len(overdue) == 1 and "alex · !475" in overdue[0]
          and cols["Wartet auf andere"][0]["wait"] == "alex · !475"
          and cols["Wartet auf andere"][0]["wait_since"] == old
          and not cols["Wartet auf andere"][0]["body"])
    check("eskalation: undatierter Wait wird heute gestempelt",
          stamped == 1 and cols["Wartet auf andere"][2]["wait_since"] == today.isoformat())
    # Zweiter Lauf auf demselben Board: nichts bewegt sich mehr → kein Write, aber
    # weiterhin als überfällig gemeldet (sonst schriebe der Sweep board.md jede Nacht neu).
    overdue2, stamped2, changed2 = sweep.escalate_waits(b, today)
    check("eskalation: idempotent — zweiter Lauf meldet, ändert aber nichts",
          len(overdue2) == 1 and stamped2 == 0 and not changed2)


def test_journal_recovery() -> None:
    """Härtung: eine fertige Antwort darf ein Server-Neustart nicht mehr verschlucken.
    ready-Journal → nachgetragen; toter Prozess mit stdout → geerntet; Item nicht mehr
    offen → Journal weggeräumt statt doppelt gepostet."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    base = f"http://127.0.0.1:{port}"
    try:
        with tempfile.TemporaryDirectory() as td:
            jd = Path(td)
            # (1) Run war fertig (status=ready), aber der Append kam nie an (Server tot)
            (jd / "run-bbbbbbbbbbbb-1.meta.json").write_text(json.dumps({
                "gc_id": "bbbbbbbbbbbb", "title": "Offener Faden", "status": "ready",
                "reply_text": "gerettete antwort", "session": "fa4e5e55-0000-4000-8000-00000000e2e1 · board-x",
                "pid": 1, "started": 0, "timeout": 900}))
            notes = gc_runner.recover_journals(base, journal_dir=jd)
            text = Path(tmp).read_text()
            check("recover: ready-Antwort landet im Faden", "@gc-re: gerettete antwort" in text)
            check("recover: Session mitgeschrieben", "@gc-session: fa4e5e55-0000-4000-8000-00000000e2e1 · board-x" in text)
            check("recover: Journal weggeräumt", not list(jd.glob("*.meta.json")) and len(notes) == 1)

            # (2) Prozess tot, aber claude hatte stdout schon zu Ende geschrieben → ernten
            (jd / "run-aaaaaaaaaaaa-2.meta.json").write_text(json.dumps({
                "gc_id": "aaaaaaaaaaaa", "title": "Offener Faden", "status": "running",
                "reply_text": "", "session": "", "pid": 999999, "started": 0, "timeout": 900}))
            (jd / "run-aaaaaaaaaaaa-2.out.json").write_text(json.dumps({
                "result": "aus dem journal geerntet", "session_id": "fa4e5e55-0000-4000-8000-00000000e2e2",
                "permission_denials": [], "subtype": "success", "is_error": False}))
            gc_runner.recover_journals(base, journal_dir=jd)
            check("recover: stdout eines toten Runs wird geerntet",
                  "@gc-re: aus dem journal geerntet" in Path(tmp).read_text())

            # (3) Idempotenz: Journal für ein Item, das nicht (mehr) auf GC wartet → nur wegräumen
            (jd / "run-aaaaaaaaaaaa-3.meta.json").write_text(json.dumps({
                "gc_id": "aaaaaaaaaaaa", "title": "Offener Faden", "status": "ready",
                "reply_text": "darf NICHT doppelt kommen", "session": "", "pid": 1,
                "started": 0, "timeout": 900}))
            gc_runner.recover_journals(base, journal_dir=jd)
            final = Path(tmp).read_text()
            check("recover: kein Doppel-Post auf beantwortetes Item",
                  "darf NICHT doppelt kommen" not in final and not list(jd.glob("*.meta.json")))
            check("recover: Board bleibt verlustfrei", server.lost_total(final, server.parse_board(final)) == 0)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_timeout_default() -> None:
    """Timeout-Modell seit 2026-07-27 (Owner: „machen wir erstmal 15/60"), auf 120/15/45
    erweitert am 18.08.: die harte Stoppuhr ist zur NOTBREMSE geworden, gekillt wird
    primär bei Stillstand — und seit 18.08. auch bei einem hängenden Werkzeug.
    Alle Zahlen hier festnageln — sie sind eine Entscheidung, kein Zufall."""
    check("timeout: Notbremse 120 min", gc_runner.DEFAULT_TIMEOUT == 7200)
    check("timeout: Long-Run-Notbremse 6 h", gc_runner.LONG_TIMEOUT == 21600)
    check("timeout: Stillstand 15 min", gc_runner.IDLE_TIMEOUT == 900)
    check("timeout: Werkzeug-Frist 15 min", gc_runner.BUSY_TIMEOUT == 900)
    check("timeout: Sub-Agenten-Frist 45 min", gc_runner.BUSY_TIMEOUT_AGENT == 2700)
    check("timeout: Fristen steigen von Stillstand bis Notbremse",
          gc_runner.IDLE_TIMEOUT <= gc_runner.BUSY_TIMEOUT
          < gc_runner.BUSY_TIMEOUT_AGENT < gc_runner.DEFAULT_TIMEOUT < gc_runner.LONG_TIMEOUT)


def test_stream_parser_beide_formate() -> None:
    """Der Parser muss BEIDE Ausgabeformate verstehen — und aus einem ABGESCHNITTENEN
    Strom noch die session_id retten. Letzteres ist der eigentliche Gewinn der Umstellung:
    bis 2026-07-27 stand im Faden „Session ist evtl. resumebar", gespeichert wurde aber
    nichts, weil ein gekillter Run kein geparstes JSON hinterließ."""
    ok_env = ('{"type":"result","subtype":"success","result":"hi","session_id":'
              '"fa4e5e55-0000-4000-8000-00000000e2e1","permission_denials":[]}')
    old = gc_runner._parse_claude_stdout(ok_env, "", 0)
    check("stream: altes Einzel-JSON weiter geparst", old["ok"] and old["reply"] == "hi")

    stream = "\n".join([
        '{"type":"system","subtype":"init","session_id":"fa4e5e55-0000-4000-8000-00000000e2e1","model":"m"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","content":"x"}]}}',
        ok_env,
    ])
    new = gc_runner._parse_claude_stdout(stream, "", 0)
    check("stream: JSONL → letztes result gewinnt", new["ok"] and new["reply"] == "hi"
          and new["session_id"] == "fa4e5e55-0000-4000-8000-00000000e2e1")

    cut = ('{"type":"system","subtype":"init","session_id":"fa4e5e55-0000-4000-8000-00000000e2e1"}\n'
           '{"type":"assistant","mess')  # mitten im Schreiben gekillt
    rescue = gc_runner._parse_claude_stdout(cut, "", None)
    check("stream: session_id überlebt den Abbruch (Resume bleibt möglich)",
          not rescue["ok"] and rescue["session_id"] == "fa4e5e55-0000-4000-8000-00000000e2e1")

    tail = gc_runner.StreamTail(Path("/nonexistent"))
    for line in stream.splitlines():
        tail._absorb(line)
    check("stream: StreamTail zählt Schritte + merkt das Werkzeug",
          tail.state["steps"] == 1 and tail.state["last_tool"] == "Read"
          and tail.state["session_id"] == "fa4e5e55-0000-4000-8000-00000000e2e1")
    tail._absorb('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}')
    check("stream: 'allowed' ist kein Rate-Limit-Signal", tail.state["rate_limit"] == "")
    tail._absorb('{"type":"rate_limit_event","rate_limit_info":{"status":"throttled"}}')
    check("stream: echtes Rate-Limit wird gemerkt", tail.state["rate_limit"] == "throttled")

    # busy-Buchführung: offener Werkzeugaufruf = „arbeitet", nicht „hängt" (Review-F1)
    t2 = gc_runner.StreamTail(Path("/nonexistent"))
    t2._absorb('{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash"}]}}')
    check("stream: offener Werkzeugaufruf zählt als beschäftigt",
          t2.state["busy"] == 1 and t2.state["busy_tool"] == "Bash")
    t2._absorb('{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1"}]}}')
    check("stream: zurückgekehrtes Werkzeug beendet den Beschäftigt-Zustand",
          t2.state["busy"] == 0 and t2.state["busy_tool"] == "")
    # parallele Werkzeuge: erst wenn ALLE zurück sind, ist der Agent wieder untätig
    t2._absorb('{"type":"assistant","message":{"content":[{"type":"tool_use","id":"a","name":"Read"}]}}')
    t2._absorb('{"type":"assistant","message":{"content":[{"type":"tool_use","id":"b","name":"Read"}]}}')
    t2._absorb('{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"a"}]}}')
    check("stream: parallele Werkzeuge werden einzeln verbucht", t2.state["busy"] == 1)
    # Werkzeug-Fristen: normales Werkzeug offen → kurze Frist; sobald ein Sub-Agent
    # dabei ist, gilt die lange — auch NEBEN einem normalen Werkzeug (Maximum zählt).
    check("stream: busy_budget normal = Werkzeug-Frist", t2.busy_budget() == gc_runner.BUSY_TIMEOUT)
    t2._absorb('{"type":"assistant","message":{"content":[{"type":"tool_use","id":"c","name":"Agent"}]}}')
    check("stream: offener Sub-Agent hebt die Frist an",
          t2.busy_budget() == gc_runner.BUSY_TIMEOUT_AGENT)

    # Ein Strom, der nach genau EINEM Ereignis abbrach, ist ein gültiges Einzelobjekt —
    # darf aber nicht als Ergebnis-Envelope durchgehen (sonst: „subtype=init" als Fehler).
    only_init = '{"type":"system","subtype":"init","session_id":"fa4e5e55-0000-4000-8000-00000000e2e1"}'
    r = gc_runner._parse_claude_stdout(only_init, "", None)
    check("stream: einzelnes init ist kein Ergebnis, rettet aber die Session",
          not r["ok"] and "no result" in r["raw_error"]
          and r["session_id"] == "fa4e5e55-0000-4000-8000-00000000e2e1")


def test_watch_run_stillstand_vs_arbeit() -> None:
    """Der Kern der Umstellung: ein ARBEITENDER Agent darf nicht mehr an der Uhr sterben,
    ein hängender schon. Echte Subprozesse, keine Mocks — genau hier lag der Fehler, den
    Mocks nie gezeigt hätten (8 gekillte Runs am 27.07.)."""
    old_idle, old_poll = gc_runner.IDLE_TIMEOUT, gc_runner.POLL_EVERY
    old_busy, old_busy_agent = gc_runner.BUSY_TIMEOUT, gc_runner.BUSY_TIMEOUT_AGENT
    gc_runner.IDLE_TIMEOUT, gc_runner.POLL_EVERY = 3, 1
    gc_runner.BUSY_TIMEOUT, gc_runner.BUSY_TIMEOUT_AGENT = 10, 20
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)

            def go(body: str, cap: int = 60, stop_after: float | None = None):
                out = d / f"o{time.time()}.jsonl"
                stop = d / f"s{time.time()}.stop"
                with open(out, "w+") as fo:
                    # start_new_session wie in spawn_claude — sonst läge das Kind in
                    # UNSERER Prozessgruppe und ein Gruppen-Kill würde den Testlauf
                    # selbst erschlagen (genau so beim ersten Lauf passiert).
                    p = subprocess.Popen([sys.executable, "-u", "-c", body],
                                         stdout=fo, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                    if stop_after is not None:
                        threading.Timer(stop_after, lambda: stop.write_text("x")).start()
                    return gc_runner.watch_run(p, gc_runner.StreamTail(out), cap, stop, None)

            busy = ('import time,json\n'
                    'for _ in range(8):\n'
                    '    print(json.dumps({"type":"assistant","session_id":"s",'
                    '"message":{"content":[{"type":"tool_use","name":"Read"}]}}), flush=True)\n'
                    '    time.sleep(1)\n')
            reason, elapsed = go(busy)
            check("watch: arbeitender Agent überlebt weit über das Stillstand-Limit",
                  reason == "" and elapsed > gc_runner.IDLE_TIMEOUT)

            hang = 'import time,json\nprint(json.dumps({"type":"system","subtype":"init","session_id":"s"}), flush=True)\ntime.sleep(60)\n'
            reason, elapsed = go(hang)
            check("watch: echter Stillstand wird gekillt", reason == "idle" and elapsed < 15)

            reason, _ = go(busy, cap=2)
            check("watch: Notbremse greift unabhängig vom Ereignisstrom", reason == "cap")

            reason, elapsed = go(hang, stop_after=1.5)
            check("watch: Stopp-Marke bricht ab und meldet 'stop'", reason == "stop" and elapsed < 8)

            # DER Fall, der den ganzen Umbau fast wertlos gemacht hätte (Review-Fund F1,
            # empirisch bestätigt 2026-07-27): während ein Werkzeug arbeitet, ist der
            # Ereignisstrom KOMPLETT still — ein `sleep 100` im Bash-Tool erzeugte 110 s
            # lang 0 Byte. Ohne die busy-Buchführung stürbe hier ein arbeitender Agent.
            busy_tool = ('import time,json\n'
                         'print(json.dumps({"type":"system","subtype":"init","session_id":"s"}), flush=True)\n'
                         'print(json.dumps({"type":"assistant","message":{"content":['
                         '{"type":"tool_use","id":"t1","name":"Bash"}]}}), flush=True)\n'
                         'time.sleep(9)\n'   # Werkzeug arbeitet, Strom schweigt — 3x IDLE_TIMEOUT
                         'print(json.dumps({"type":"user","message":{"content":['
                         '{"type":"tool_result","tool_use_id":"t1"}]}}), flush=True)\n')
            reason, elapsed = go(busy_tool)
            check("watch: langer STILLER Werkzeugaufruf wird NICHT als Stillstand gekillt",
                  reason == "" and elapsed > gc_runner.IDLE_TIMEOUT * 2)

            # Seit 08/2026 ist die Werkzeug-Wartezeit ENDLICH: ein Werkzeug, das
            # busy_budget lang keinen Mucks macht, gilt als hängend — vorher überlebte
            # genau dieser Fall bis zur Notbremse.
            hung_tool = ('import time,json\n'
                         'print(json.dumps({"type":"system","subtype":"init","session_id":"s"}), flush=True)\n'
                         'print(json.dumps({"type":"assistant","message":{"content":['
                         '{"type":"tool_use","id":"t1","name":"Bash"}]}}), flush=True)\n'
                         'time.sleep(60)\n')  # weit über BUSY_TIMEOUT (10 s im Test)
            reason, elapsed = go(hung_tool)
            check("watch: still hängendes Werkzeug stirbt an der Werkzeug-Frist",
                  reason == "hung" and elapsed < 20)

            # Sub-Agent offen → lange Frist: überlebt Funkstille ÜBER der normalen
            # Werkzeug-Frist, weil Sub-Agenten legitim lange still arbeiten dürfen.
            agent_tool = ('import time,json\n'
                          'print(json.dumps({"type":"system","subtype":"init","session_id":"s"}), flush=True)\n'
                          'print(json.dumps({"type":"assistant","message":{"content":['
                          '{"type":"tool_use","id":"t1","name":"Agent"}]}}), flush=True)\n'
                          'time.sleep(13)\n'  # > BUSY_TIMEOUT (10), < BUSY_TIMEOUT_AGENT (20)
                          'print(json.dumps({"type":"user","message":{"content":['
                          '{"type":"tool_result","tool_use_id":"t1"}]}}), flush=True)\n')
            reason, elapsed = go(agent_tool)
            check("watch: offener Sub-Agent bekommt die lange Frist",
                  reason == "" and elapsed > gc_runner.BUSY_TIMEOUT)

            # Gegenprobe: kommt das tool_result zurück und DANN passiert nichts mehr,
            # greift der Stillstand wieder — sonst wäre die Regel ein Freibrief.
            then_hang = ('import time,json\n'
                         'print(json.dumps({"type":"system","subtype":"init","session_id":"s"}), flush=True)\n'
                         'print(json.dumps({"type":"assistant","message":{"content":['
                         '{"type":"tool_use","id":"t1","name":"Bash"}]}}), flush=True)\n'
                         'print(json.dumps({"type":"user","message":{"content":['
                         '{"type":"tool_result","tool_use_id":"t1"}]}}), flush=True)\n'
                         'time.sleep(60)\n')
            reason, elapsed = go(then_hang)
            check("watch: nach Rückkehr des Werkzeugs zählt der Stillstand wieder",
                  reason == "idle" and elapsed < 15)
    finally:
        gc_runner.IDLE_TIMEOUT, gc_runner.POLL_EVERY = old_idle, old_poll
        gc_runner.BUSY_TIMEOUT, gc_runner.BUSY_TIMEOUT_AGENT = old_busy, old_busy_agent


def test_kill_trifft_kindergruppe_nicht_uns_selbst() -> None:
    """Der Kill muss die Kinder des Agenten mitnehmen (Bash-Tool, MCP-Server), darf aber
    NIEMALS die eigene Prozessgruppe treffen — das wäre der Board-Server selbst.
    Beim ersten Lauf dieser Tests ist genau das passiert: der Testprozess hat sich
    kommentarlos mitsamt Shell beendet. Deshalb hier festgenagelt."""
    import signal as _sig

    # 1) Kind in EIGENER Gruppe: Enkel muss mitsterben.
    body = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "time.sleep(120)\n")
    p = subprocess.Popen([sys.executable, "-c", body], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    pgid = os.getpgid(p.pid)
    check("kill: Kind läuft in eigener Gruppe (Voraussetzung)", pgid != os.getpgid(0))
    gc_runner._kill_proc(p)
    check("kill: Kindprozess ist tot", p.poll() is not None)
    time.sleep(0.5)
    try:
        os.killpg(pgid, 0)
        gruppe_weg = False
    except (OSError, ProcessLookupError):
        gruppe_weg = True
    check("kill: die ganze Gruppe ist weg (Enkel überleben nicht)", gruppe_weg)

    # 2) Kind in UNSERER Gruppe: nur der Prozess stirbt, wir überleben.
    p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    check("kill: Testprozess liegt in derselben Gruppe (der gefährliche Fall)",
          os.getpgid(p2.pid) == os.getpgid(0))
    gc_runner._kill_proc(p2)
    check("kill: auch dann stirbt nur das Kind", p2.poll() is not None)
    check("kill: WIR leben noch — kein Selbstmord der eigenen Gruppe", True)
    _ = _sig  # nur zur Doku, welches Modul hier im Spiel ist


def test_kill_outcome_und_stop_endpunkt() -> None:
    """Abbruch-Texte + der Stopp-Endpunkt. Ein selbst gedrückter Stopp darf NICHT wie ein
    Absturz aussehen (⏹ statt ❌) und muss den Resume-Handle mitgeben."""
    state = {"session_id": "fa4e5e55-0000-4000-8000-00000000e2e1", "steps": 7,
             "last_tool": "Bash", "rate_limit": ""}
    stopped = gc_runner._kill_outcome("stop", 300, state, 3600)
    check("kill: Stopp trägt ⏹, nicht ❌", stopped["raw_error"].startswith("⏹")
          and "Stopped" in stopped["raw_error"])
    check("kill: Stopp nennt Fortschritt + Resume", "7 steps" in stopped["raw_error"]
          and "session saved" in stopped["raw_error"])
    idle = gc_runner._kill_outcome("idle", 1000, {**state, "session_id": ""}, 3600)
    check("kill: Stillstand ist ein ❌ und sagt das auch", idle["raw_error"].startswith("❌")
          and "no activity" in idle["raw_error"])
    check("kill: ohne Handle wird das ehrlich gesagt", "no session handle" in idle["raw_error"])

    # Notbremse: die Kappe ist nicht immer die echte Laufzeit (schlafender Mac).
    knapp = gc_runner._kill_outcome("cap", 3620, state, 3600)
    check("kill: Notbremse im Normalfall ohne Zusatz", "actually" not in knapp["raw_error"])
    spaet = gc_runner._kill_outcome("cap", 6042, state, 3600)
    check("kill: deutlich späterer Abbruch nennt die echte Laufzeit",
          "actually 100 min" in spaet["raw_error"])

    # _outcome darf kein zweites „fehlgeschlagen" davorsetzen, und der Stempel wird ⏹
    with tempfile.TemporaryDirectory() as td:
        text, sess, last = gc_runner._outcome(stopped, "aaaaaaaaaaaa", "T", Path(td))
    check("kill: Faden-Text bleibt der Abbruch-Wortlaut", text.startswith("⏹")
          and "Agent run failed" not in text)
    check("kill: Stempel ist ⏹, nicht ❌", last.startswith("⏹ · "))
    check("kill: Session landet im Faden (damit Resume wirklich geht)",
          sess.startswith("fa4e5e55-0000-4000-8000-00000000e2e1"))

    # Endpunkt: ohne laufenden Run 409, mit Stopp-Pfad wird die Marke geschrieben
    check("stop: unbekannte id → kein Stopp", server.request_stop("ffffffffffff") != "")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "run.stop"
        with server.RUN_LOCK:
            server.RUNNING["cccccccccccc"] = time.time()
            server.BEATS["cccccccccccc"] = {"stop_path": str(marker)}
        try:
            check("stop: laufender Run nimmt den Stopp an", server.request_stop("cccccccccccc") == "")
            check("stop: Marke liegt da, wo die Wache sie sucht", marker.exists())
        finally:
            with server.RUN_LOCK:
                server.RUNNING.pop("cccccccccccc", None)
                server.BEATS.pop("cccccccccccc", None)
    check("stop: interne Pfade gehen NICHT an den Browser",
          all("stop_path" not in v for v in server._public_beats().values()))


def test_stream_view_codex_ereignisse() -> None:
    """Codex-Runs waren im Ereignis-Panel BLIND: der Zeilen-Bauer kannte nur claudes
    Ereignisnamen, also ergaben 67 echte Codex-Ereignisse null Zeilen (gemessen 12.08. am
    laufenden Run). Fixture-Zeilen hier sind aus einem echten `codex exec --json`-Strom
    abgeschrieben, nicht erfunden — der Fehler war ja gerade, dass ein grüner Test mit
    ausgedachten Daten nichts merkte."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "run-cccccccccccc-20260812-171110-d052.out.json").write_text("\n".join([
            '{"type":"thread.started","thread_id":"019ff687-1426-74f3-abdd-10c8d62f6946"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"Ich bin Nox."}}',
            '{"type":"item.started","item":{"id":"i1","type":"command_execution",'
            '"command":"/bin/zsh -lc \\"wc -l context/README.md\\""}}',
            '{"type":"item.completed","item":{"id":"i1","type":"command_execution",'
            '"command":"/bin/zsh -lc \\"wc -l context/README.md\\"","aggregated_output":"235 context/README.md"}}',
            '{"type":"item.started","item":{"id":"i2","type":"file_change","changes":'
            '[{"path":"/x/board.md","kind":"update"}]}}',
            '{"type":"item.completed","item":{"id":"i2","type":"file_change","changes":'
            '[{"path":"/x/board.md","kind":"update"}],"status":"completed"}}',
            '{"type":"item.started","item":{"id":"i3","type":"reasoning","text":"still"}}',
            '{"type":"turn.completed","usage":{"input_tokens":26654,"output_tokens":812}}',
        ]))
        v = server.stream_view(d, "cccccccccccc", True)
        kinds = [r["kind"] for r in v["rows"]]
        check("codex-view: der Strom ergibt überhaupt Zeilen (vorher: null)", len(v["rows"]) >= 7)
        check("codex-view: Start, Antwort, Werkzeug, Ergebnis und Abschluss sind da",
              kinds[0] == "start" and "say" in kinds and "tool" in kinds
              and "result" in kinds and kinds[-1] == "done")
        check("codex-view: turn.started und reasoning bleiben Rauschen",
              not any("still" in str(r.get("text", "")) for r in v["rows"]))
        tools = [r.get("tool") for r in v["rows"] if r["kind"] == "tool"]
        check("codex-view: die zsh-Hülle steht nicht im Werkzeugnamen",
              "shell: wc" in tools and not any("zsh -lc" in str(t) for t in tools))
        check("codex-view: Dateiänderung nennt Anzahl und Datei",
              any(str(t).startswith("file_change") for t in tools)
              and any("update board.md" in str(r.get("text", "")) for r in v["rows"]))

    # Ein fehlgeschlagener Turn muss als FEHLER durchkommen, nicht still verschwinden.
    fail = server._stream_row({"type": "turn.failed", "error": {"message": "usage limit"}})
    check("codex-view: turn.failed wird zur Fehlerzeile",
          fail["kind"] == "result" and fail["error"] and "usage limit" in fail["text"])
    # Und die Weiche darf claude nicht anfassen: gleiche Eingabe, gleiches Ergebnis wie bisher.
    claude = server._stream_row({"type": "result", "subtype": "success", "num_turns": 3})
    check("codex-view: claude-Ereignisse laufen unverändert durch",
          claude["kind"] == "done" and "3 turns" in claude["text"])


def test_stream_view_opencode_ereignisse() -> None:
    """OpenCode writes a valid JSONL stream whose event names match neither Claude
    nor Codex. Without its own adapter, the live panel stayed on "loading…" even
    though the runner was working (measured 23.08. on three real OpenCode journals)."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        events = [
            {"type": "step_start", "sessionID": "ses_realshape", "part": {
                "type": "step-start"}},
            {"type": "tool_use", "sessionID": "ses_realshape", "part": {
                "type": "tool", "tool": "read", "state": {"status": "completed",
                    "input": {"filePath": "/repo/README.md"}, "output": "contents"}}},
            {"type": "text", "sessionID": "ses_realshape", "part": {
                "type": "text", "text": "I found the cause."}},
            {"type": "step_finish", "sessionID": "ses_realshape", "part": {
                "type": "step-finish", "tokens": {"total": 321}}},
        ]
        (d / "run-eeeeeeeeeeee-20260823-214804-4ba0.out.json").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n")
        view = server.stream_view(d, "eeeeeeeeeeee", True)
        check("opencode-view: erster Schritt ersetzt den Loading-Platzhalter",
              view["rows"][0] == {"kind": "start", "text": "Started · OpenCode", "n": 1})
        check("opencode-view: Werkzeug samt Eingabe wird lesbar",
              view["rows"][1]["kind"] == "tool" and view["rows"][1]["tool"] == "read"
              and "README.md" in view["rows"][1]["text"])
        check("opencode-view: Text und Schrittabschluss werden lesbar",
              [row["kind"] for row in view["rows"]] == ["start", "tool", "say", "result"]
              and "321 tokens" in view["rows"][-1]["text"])

    denied = server._stream_row({"type": "tool_use", "sessionID": "ses_realshape",
        "part": {"tool": "bash", "state": {"status": "denied",
            "error": "permission denied"}}})
    check("opencode-view: verweigerte Werkzeuge sind Fehlerzeilen", denied["error"] is True)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = d / "run-ffffffffffff-20260824-131557-5cae.out.json"
        src.write_text("")
        src.with_name(src.name.removesuffix(".out.json") + ".meta.json").write_text(
            json.dumps({"model": "opencode-deepseek-pro"}))
        waiting = server.stream_view(d, "ffffffffffff", True)
        check("opencode-view: leerer Live-Strom nennt Profil und wartet ehrlich",
              waiting["waiting"] is True
              and waiting["profile"] == "opencode-deepseek-pro"
              and waiting["rows"] == [])

        killed = d / "killed"
        killed.mkdir()
        (killed / "run-eeeeeeeeeeee-old.stop.jsonl").write_text(
            '{"type":"system","subtype":"init"}\n')
        gap = server.stream_view(d, "eeeeeeeeeeee", True)
        check("opencode-view: Startup-Lücke zeigt nie alten Stop-Strom",
              gap["waiting"] is True and gap["note"] == "running now"
              and gap["rows"] == [] and "aborted" not in gap["note"])


def test_stream_view_und_kill_log() -> None:
    """Die Einsicht in den Ereignisstrom (Bens „was macht er gerade") und das Kill-Log."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "run-dddddddddddd-20260727-120000-aaaa.out.json").write_text("\n".join([
            '{"type":"system","subtype":"hook_started","hook_name":"x"}',
            '{"type":"system","subtype":"init","session_id":"s","model":"claude-opus-5"}',
            '{"type":"system","subtype":"thinking_tokens","estimated_tokens":5}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/a/b"}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","content":"inhalt"}]}}',
            '{"type":"result","subtype":"success","num_turns":3}',
        ]))
        v = server.stream_view(d, "dddddddddddd", True)
        kinds = [r["kind"] for r in v["rows"]]
        check("view: Hook- und Thinking-Rauschen fliegt raus", "start" in kinds and len(v["rows"]) == 4)
        check("view: Werkzeugname steht drin", any(r.get("tool") == "Read" for r in v["rows"]))
        check("view: laufender Run wird als solcher beschriftet", v["note"] == "running now")
        check("view: nichts da → ehrliche Meldung statt leerer Liste",
              server.stream_view(d, "eeeeeeeeeeee", False)["empty"] != "")

        # Kill-Log + aufgehobener Strom
        old_log = gc_runner.KILL_LOG
        gc_runner.KILL_LOG = d / "killed-runs.jsonl"
        try:
            gc_runner.log_kill("dddddddddddd", "Titel", "opus", "idle", 930,
                               {"steps": 12, "last_tool": "Bash", "session_id": "s"},
                               d / "run-dddddddddddd-20260727-120000-aaaa.out.json")
            rows = [json.loads(x) for x in gc_runner.KILL_LOG.read_text().splitlines()]
            check("kill-log: Zeile geschrieben mit Grund + Fortschritt",
                  rows[-1]["reason"] == "idle" and rows[-1]["steps"] == 12
                  and rows[-1]["elapsed_min"] == 15.5)
            kept = list((d / "killed").glob("*.jsonl"))
            check("kill-log: Ereignisstrom bleibt zur Nachschau liegen", len(kept) == 1)
            # Genau der Fall, um den es geht: das Journal ist weg (Run abgeräumt), der
            # aufgehobene Strom des Abbruchs muss trotzdem noch einsehbar sein.
            (d / "run-dddddddddddd-20260727-120000-aaaa.out.json").unlink()
            after = server.stream_view(d, "dddddddddddd", False)
            check("kill-log: gekillter Strom bleibt nach dem Abräumen lesbar",
                  len(after["rows"]) == 4 and "aborted run" in after["note"])
            check("kill-log: killed_today findet den Eintrag",
                  any(r["gc_id"] == "dddddddddddd" for r in server.killed_today(d)))
            # Der Mitternachts-Rollover des Caches hängt nebenan in
            # test_cache_consistency.py — dort leben die Cache-Invarianten.
        finally:
            gc_runner.KILL_LOG = old_log
            server._KILL_CACHE.update(mtime=-1.0, day="", rows=[])


def test_sse_stream_endpoint() -> None:
    """Phase 3: /api/gc-stream-sse — Header, inkrementeller Push, Ende schließt.

    Bewusst über echtes HTTP statt Funktionsaufruf: der eigentliche Neuanteil ist der
    Schreibweg an _send() vorbei (kein Content-Length, offene Verbindung, Häppchen).
    Der Inkrementell-Beweis ist der harte Teil: nach dem ersten Event wird der bereits
    konsumierte Dateianfang mit einem GÜLTIGEN Köder-Event gleicher Länge überschrieben.
    Läse der Server bei jedem Push die Datei neu, käme der Köder als Zeile an — beim
    Byte-Offset-Tail darf er nie auftauchen."""
    import http.client

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "journal").mkdir()
        src = d / "journal" / "run-abcabcabcabc-20260810-120000-aaaa.out.json"
        src.write_text(
            '{"type":"system","subtype":"init","session_id":"s","model":"claude-opus-5"}\n'
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/a/b"}}]}}\n')
        board = d / "board.md"
        board.write_text(SYNTH)
        old_root, old_journal = server.ROOT, server.JOURNAL_DIR
        server.ROOT, server.JOURNAL_DIR = d, d / "journal"
        with server.RUN_LOCK:
            server.RUNNING["abcabcabcabc"] = time.time()
        httpd, port = _serve(board)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("GET", "/api/gc-stream-sse?id=abcabcabcabc")
            res = conn.getresponse()
            check("sse: 200 + text/event-stream",
                  res.status == 200 and res.getheader("Content-Type") == "text/event-stream")
            check("sse: KEIN Content-Length — long-lived, an _send() vorbei",
                  res.getheader("Content-Length") is None)
            check("sse: Cache-Control no-cache", res.getheader("Cache-Control") == "no-cache")
            check("sse: X-Accel-Buffering no", res.getheader("X-Accel-Buffering") == "no")

            def read_event() -> str:
                """Liest bis zur Leerzeile = ein SSE-Event. '' = Server hat zugemacht."""
                lines: list[str] = []
                while True:
                    ln = res.readline()
                    if not ln:
                        return ""
                    ln = ln.decode().rstrip("\n")
                    if ln == "":
                        if lines:
                            return "\n".join(lines)
                        continue
                    lines.append(ln)

            first = read_event()
            check("sse: Backlog kommt als erstes Event, id = Byte-Offset",
                  first.startswith("id: ") and '"Read"' in first)
            off1 = int(first.split("\n")[0][4:])
            check("sse: Offset zeigt hinter die letzte komplette Zeile",
                  off1 == src.stat().st_size)

            # Konsumierten Anfang mit einem Köder-Event GLEICHER Länge überschreiben:
            # gültiges JSON, das als 💬-Zeile gerendert würde — es darf nie ankommen.
            koeder = '{"type":"assistant","message":{"content":[{"type":"text","text":"KOEDER"}]}}'
            koeder = koeder + " " * (off1 - len(koeder) - 1) + "\n"
            with open(src, "r+b") as f:
                f.write(koeder.encode())
            with open(src, "ab") as f:
                f.write(b'{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"ok"}]}}\n')
            second = read_event()
            check("sse: nur das Delta kommt — überschriebener Anfang wird NICHT neu gelesen",
                  '"result"' in second and "KOEDER" not in second)

            # Ein `done` MITTEN im Strom darf NICHT schließen (Fund 12.08. am lebenden
            # Objekt): eine Journal-Datei sammelt mehrere Turns derselben Session, ein
            # abgeschlossener Vorgänger liegt also als done-Zeile mitten drin. Wer daraus
            # das Ende ableitet, macht nach dem Aufholen sofort zu — und weil das Frontend
            # dann still aufs Polling zurückfällt, sieht der Fehler aus wie Erfolg.
            with open(src, "ab") as f:
                f.write(b'{"type":"result","subtype":"success","num_turns":2}\n')
            third = read_event()
            check("sse: done mitten im Strom schließt NICHT — Registry entscheidet",
                  '"done"' in third and "event: end" not in third)
            with open(src, "ab") as f:
                f.write(b'{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t9","name":"Edit","input":{}}]}}\n')
            check("sse: nach dem done fließt der Strom weiter", '"Edit"' in read_event())

            # Erst wenn die Registry den Run vergisst, ist Schluss.
            with server.RUN_LOCK:
                server.RUNNING.pop("abcabcabcabc", None)
            rest = res.read()  # blockiert bis close — DASS es zurückkehrt, ist der Test
            check("sse: Run aus der Registry → end-Event, dann schließt der Server",
                  b"event: end" in rest)
        finally:
            server.ROOT, server.JOURNAL_DIR = old_root, old_journal
            with server.RUN_LOCK:
                server.RUNNING.pop("abcabcabcabc", None)
            httpd.shutdown()


def test_sse_reconnect_und_ohne_strom() -> None:
    """Last-Event-ID → Byte-Offset: nach Reconnect kommt nichts doppelt. Und ohne
    Strom-Datei schließt der Endpoint sofort mit end (Frontend fällt auf Polling
    zurück, das auch die aufgehobenen killed/-Ströme kennt)."""
    import http.client

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "journal").mkdir()
        line1 = '{"type":"system","subtype":"init","session_id":"s","model":"m"}\n'
        src = d / "journal" / "run-bcdbcdbcdbcd-20260810-120000-aaaa.out.json"
        src.write_text(
            line1
            + '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Grep","input":{"pattern":"x"}}]}}\n')
        board = d / "board.md"
        board.write_text(SYNTH)
        old_root, old_journal = server.ROOT, server.JOURNAL_DIR
        server.ROOT, server.JOURNAL_DIR = d, d / "journal"
        with server.RUN_LOCK:
            server.RUNNING["bcdbcdbcdbcd"] = time.time()
        httpd, port = _serve(board)
        try:
            # Reconnect mitten im Strom: Last-Event-ID = Offset hinter Zeile 1.
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("GET", "/api/gc-stream-sse?id=bcdbcdbcdbcd",
                         headers={"Last-Event-ID": str(len(line1.encode()))})
            res = conn.getresponse()
            buf = b""
            while b"\n\n" not in buf:
                chunk = res.readline()
                if not chunk:
                    break
                buf += chunk
            text = buf.decode()
            check("sse: Reconnect liefert nur Zeilen NACH dem Offset (kein Doppel)",
                  '"Grep"' in text and "Start" not in text)
            conn.close()

            # Heartbeat: schweigt der Run, muss trotzdem regelmäßig etwas über die
            # Leitung gehen — sonst fällt ein toter Socket (Tab zu, Mac im Sleep) erst
            # beim nächsten echten Event auf, und das kann Stunden dauern.
            old_hb = server.SSE_HEARTBEAT_S
            server.SSE_HEARTBEAT_S = 1
            try:
                conn3 = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                conn3.request("GET", "/api/gc-stream-sse?id=bcdbcdbcdbcd")
                res3 = conn3.getresponse()
                seen = b""
                for _ in range(40):          # bis zum ersten Kommentar-Ping lesen
                    ln = res3.readline()
                    if not ln:
                        break
                    seen += ln
                    if ln.strip() == b":":
                        break
                check("sse: Heartbeat-Ping bei schweigendem Run", b":\n" in seen)
                conn3.close()
            finally:
                server.SSE_HEARTBEAT_S = old_hb

            # Unbekannte (aber formal gültige) id: kein Journal → sofort end + close.
            conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn2.request("GET", "/api/gc-stream-sse?id=eeeeeeeeeeee")
            res2 = conn2.getresponse()
            body = res2.read()
            check("sse: ohne Strom-Datei sofort end + Verbindung zu", b"event: end" in body)
            conn2.close()
        finally:
            server.ROOT, server.JOURNAL_DIR = old_root, old_journal
            with server.RUN_LOCK:
                server.RUNNING.pop("bcdbcdbcdbcd", None)
            httpd.shutdown()


def test_sse_empty_live_stream_reports_waiting_profile() -> None:
    """A real OpenCode run can hold an empty stdout file while the provider starts.
    The SSE connection must immediately replace the UI's loading placeholder and name
    the actual profile, even before the first JSONL event exists."""
    import http.client

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        journal = d / "journal"
        journal.mkdir()
        stem = "run-fafafafafafa-20260824-131557-5cae"
        board = d / "board.md"
        board.write_text(SYNTH)
        old_root, old_journal = server.ROOT, server.JOURNAL_DIR
        server.ROOT, server.JOURNAL_DIR = d, journal
        with server.RUN_LOCK:
            server.RUNNING["fafafafafafa"] = time.time()
        httpd, port = _serve(board)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/gc-stream-sse?id=fafafafafafa")
            res = conn.getresponse()
            lines: list[bytes] = []
            while True:
                line = res.readline()
                if line in (b"", b"\n"):
                    break
                lines.append(line)
            event = b"".join(lines).decode()
            check("sse-waiting: noch fehlender Live-Strom antwortet sofort",
                  '"waiting": true' in event and '"rows": []' in event)

            # The journal appears only after the UI has already connected. The same
            # SSE request must adopt it rather than ending/falling back to killed data.
            (journal / f"{stem}.out.json").write_text("")
            (journal / f"{stem}.meta.json").write_text(json.dumps({
                "gc_id": "fafafafafafa", "model": "opencode-deepseek-pro",
            }))
            lines = []
            while True:
                line = res.readline()
                if line in (b"", b"\n"):
                    break
                lines.append(line)
            event = b"".join(lines).decode()
            check("sse-waiting: echte OpenCode-Profilwahl steht im Status",
                  '"profile": "opencode-deepseek-pro"' in event
                  and '"waiting": true' in event)
        finally:
            with server.RUN_LOCK:
                server.RUNNING.pop("fafafafafafa", None)
            server.ROOT, server.JOURNAL_DIR = old_root, old_journal
            httpd.shutdown()


def test_dev_radar_ref_resolution() -> None:
    """dev_radar: Prioritätskette der Ref-Auflösung (Pin → hint:line → hint:item →
    ambiguous/unresolved) — pure Logik, aber der gefährlichste Teil des Radars:
    eine falsch aufgelöste Ref liefert plausible, aber FALSCHE Live-Daten
    (!343 existiert in mehreren Repos). Kein CLI-Call, keine Mocks nötig.

    REF_PINS/GITLAB_HINTS/GITHUB_HINTS/JIRA_PATTERN sind Instanz-Konfiguration und
    defaulten in dieser OSS-Fassung auf leer — der Test biegt sie hier auf Fixture-
    Werte um, statt sich auf eine echte, projektspezifische Konfiguration zu verlassen."""
    import dev_radar as dr

    old_pins, old_gl, old_gh = dr.REF_PINS, dr.GITLAB_HINTS, dr.GITHUB_HINTS
    old_pattern, old_re = dr.JIRA_PATTERN, dr.JIRA_RE
    try:
        dr.REF_PINS = {"gl!343": "group/repo-core"}
        dr.GITLAB_HINTS = [(r"convui", "group/repo-ui"), (r"monorepo", "group/repo-monorepo")]
        dr.GITHUB_HINTS = []
        dr.JIRA_PATTERN = r"\b(PROJ-\d{3,5})\b"
        dr.JIRA_RE = __import__("re").compile(dr.JIRA_PATTERN)

        proj, how = dr._resolve("gl", "343", "monorepo microapp zeug !343", "monorepo")
        check("radar: Pin schlägt Hint (!343 = repo-core, NICHT monorepo)",
              how == "pin" and proj.endswith("repo-core"))
        proj, how = dr._resolve("gl", "999", "convui MR !999", "irgendwas")
        check("radar: hint:line", how == "hint:line" and proj.endswith("repo-ui"))
        proj, how = dr._resolve("gl", "999", "!999", "flashcard monorepo")
        check("radar: hint:item bei eindeutigem Item", how == "hint:item" and proj.endswith("repo-monorepo"))
        proj, how = dr._resolve("gl", "999", "!999", "monorepo und convui")
        check("radar: mehrdeutiges Item → ambiguous statt raten", proj is None and how == "ambiguous")
        proj, how = dr._resolve("gl", "999", "!999", "nix passendes")
        check("radar: kein Signal → unresolved, KEIN Default-Repo", proj is None and how == "unresolved")

        refs = dr.extract_refs("MR https://gitlab.com/g/p/-/merge_requests/12", [])
        check("radar: volle URL → sicher aufgelöst",
              refs[0]["resolved_by"] == "url" and refs[0]["project"] == "g/p" and refs[0]["number"] == 12)
        refs = dr.extract_refs("X !(2026-07-13)", ["@ref: gl:foo/bar !77"])
        check("radar: @ref-Zeile gepinnt + Due-Syntax !(…) matcht nicht",
              len(refs) == 1 and refs[0]["project"] == "foo/bar" and refs[0]["resolved_by"] == "@ref")
        refs = dr.extract_refs("convui !475/!479/!481", [])
        check("radar: Slash-Liste vollständig (3 Refs, nicht nur die erste)",
              len(refs) == 3 and {r["number"] for r in refs} == {475, 479, 481})
        check("radar: Jira-Ref nur gelistet (checked=False)",
              any(r["host"] == "jira" and r["ref"] == "PROJ-4952"
                  for r in dr.extract_refs("PROJ-4952 fixen", [])))

        it1 = {"refs": dr.extract_refs("Kontext", ["nebenbei !475 erwähnt (convui)"])}
        it2 = {"refs": dr.extract_refs("convui !475 mergen", [])}
        dr.assign_owners([it1, it2])
        check("radar: Titel-Ref gewinnt Ownership (Befunde nicht doppelt)",
              it2["refs"][0]["owner"] and not it1["refs"][0]["owner"])
    finally:
        dr.REF_PINS, dr.GITLAB_HINTS, dr.GITHUB_HINTS = old_pins, old_gl, old_gh
        dr.JIRA_PATTERN, dr.JIRA_RE = old_pattern, old_re


def test_dev_radar_review_stale_zeigt_den_letzten_kommentar() -> None:
    """Regression: „Review haengt seit 12d" sagt nicht, WARUM. Steht im letzten
    Kommentar „can be tackled once the following is done: …", liegt der Ball gar nicht
    beim Owner — er sieht das nur, wenn der Kommentartext im review_stale-Befund
    mitkommt. Der Lauf, gegen den das getestet wird, hatte das ausdruecklich zugesagt
    („I'll do this regardless of what you pick") und nie geliefert."""
    import dev_radar as dr

    ref = {"ref": "!123", "host": "gl"}
    lc = {"author": "kollege", "at": "2026-07-01T10:00:00Z",
          "body": "can be tackled once the following is done:\n- migration merged"}

    # (a) Review haengt ohne CHANGES_REQUESTED
    f = dr.findings_for(ref, {"url": "u", "me": ["owner"], "state": "opened", "approved": False,
                              "approvers": [], "updated_at": "2026-07-01T10:00:00Z",
                              "last_comment": lc, "reviewers": ["kollege"]})
    stale = [x for x in f if x["type"] == "review_stale"]
    check("radar: review_stale nennt den letzten Kommentar",
          len(stale) == 1 and "can be tackled once the following is done:" in stale[0]["text"]
          and "kollege" in stale[0]["text"])

    # (b) CHANGES_REQUESTED, unbeantwortet
    f = dr.findings_for(ref, {"url": "u", "me": ["owner"], "state": "opened", "approved": False,
                              "approvers": [], "updated_at": "2026-07-01T10:00:00Z",
                              "last_comment": lc,
                              "changes_requested_by": [("kollege", "2026-07-01T10:00:00Z")]})
    stale = [x for x in f if x["type"] == "review_stale"]
    check("radar: CHANGES_REQUESTED-Befund nennt den letzten Kommentar",
          len(stale) == 1 and "can be tackled once" in stale[0]["text"])

    # (c) kein Kommentar da → kein leerer Anhang
    f = dr.findings_for(ref, {"url": "u", "me": ["owner"], "state": "opened", "approved": False,
                              "approvers": [], "updated_at": "2026-07-01T10:00:00Z",
                              "last_comment": None, "reviewers": ["kollege"]})
    stale = [x for x in f if x["type"] == "review_stale"]
    check("radar: ohne Kommentar bleibt der Befund unveraendert",
          len(stale) == 1 and "letzter Kommentar" not in stale[0]["text"])


def test_version_in_changelog() -> None:
    """Versioning-Konvention (v0.7.0): server.VERSION muss als Release-Überschrift
    im CHANGELOG stehen — Bump ohne Changelog-Eintrag wird rot."""
    log = (Path(__file__).resolve().parent / "CHANGELOG.md").read_text()
    check("version: server.VERSION hat CHANGELOG-Eintrag", f"## [{server.VERSION}]" in log)


def test_quick_capture_endpoint() -> None:
    """Schnellerfassung: POST /api/quick-capture legt sofort ein Item an (Titel = Textanfang,
    Text als erster @gc:-Turn) UND startet direkt einen Board-Agent-Run drauf — Fake-Agent-
    Antwort muss im Markdown landen.

    Zielthema: explizites `theme`/`col` gewinnt, sonst ein VORHANDENES „Inbox", sonst das
    erste Thema des Boards. Es wird KEIN zweites Thema mehr angelegt — das zerriss auf einer
    frischen Installation den bewusst flachen Ein-Kategorie-Start."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", "time.sleep(0.3)\n" + OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            long_text = "x" * 90  # länger als 60 Zeichen -> Titel muss gekürzt + Ellipse bekommen
            code, r = _post(port, "/api/quick-capture", {"text": long_text})
            check("capture: 202 accepted + id", code == 202 and r.get("ok") and r.get("id"))

            text = Path(tmp).read_text()
            check("capture: KEIN zweites Thema angelegt", "## Inbox" not in text)
            check("capture: landet im ersten Thema des Boards",
                  text.index("## Thema") < text.index("x" * 60) < text.index("### Next"))
            check("capture: Titel gekürzt auf 60 Zeichen + …", ("x" * 60 + "…") in text)
            check("capture: voller Text als @gc:-Turn", f"@gc: {long_text}" in text)

            deadline = time.time() + 15
            while time.time() < deadline and "fa4e5e55-0000-4000-8000-00000000e2e1" not in Path(tmp).read_text():
                time.sleep(0.2)
            text = Path(tmp).read_text()
            check("capture: Agent-Run beantwortet das neue Item", "@gc-re: testantwort vom agenten" in text)

            code2, r2 = _post(port, "/api/quick-capture", {"text": "  "})
            check("capture: leerer Text → 400", code2 == 400 and "text" in r2.get("error", ""))
            code3, r3 = _post(port, "/api/quick-capture", {"text": "x", "model": "quatsch"})
            check("capture: unbekanntes Modell → 400", code3 == 400 and "model" in r3.get("error", ""))
            code4, r4 = _post(port, "/api/quick-capture", {"text": "x", "col": "Quatschspalte"})
            check("capture: unbekannte Spalte → 400", code4 == 400 and "column" in r4.get("error", ""))

            # Adder-Uebergabe: Cmd/Strg+Enter im Spalten-Adder schickt die Karte in SEINE
            # Zelle, nicht in einen Fangkorb.
            code5, r5 = _post(port, "/api/quick-capture",
                              {"text": "aus dem adder", "theme": "thema", "col": "Bald", "run": False})
            check("capture: run=false → 201 ohne Lauf", code5 == 201 and r5.get("ran") is False and r5.get("id"))
            text = Path(tmp).read_text()
            # Spaltenschluessel sind intern deutsch, in der Datei stehen die englischen Namen.
            check("capture: theme+col landen in der gewaehlten Zelle",
                  text.index("### Next") < text.index("aus dem adder") < text.index("### Backlog"))

            # Selbsterklaerendes To-do aus einer leeren Cockpit-Zone: Titel + Body kommen
            # vom Aufrufer, der Lauf startet erst auf Knopfdruck.
            code6, r6 = _post(port, "/api/quick-capture",
                              {"text": "build the first intake card", "title": "Build an Intake card",
                               "body": ["context line one", "context line two"], "run": False})
            check("capture: expliziter Titel + Body", code6 == 201 and r6.get("id"))
            text = Path(tmp).read_text()
            check("capture: Body-Zeilen stehen unter dem Titel",
                  "- [ ] Build an Intake card" in text and "  context line one" in text
                  and "  context line two" in text)

            # Ein VORHANDENES „Inbox" gewinnt weiter vor dem ersten Thema — nur neu ANGELEGT
            # wird es nicht mehr.
            Path(tmp).write_text(SYNTH.replace("# Personen", "## Inbox\n\n### Jetzt\n\n### Bald\n\n### Geparkt\n\n# Personen"))
            code7, r7 = _post(port, "/api/quick-capture", {"text": "faengt im korb", "run": False})
            text = Path(tmp).read_text()
            check("capture: vorhandenes Inbox gewinnt vor dem ersten Thema",
                  code7 == 201 and text.index("## Inbox") < text.index("faengt im korb"))
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_auto_retrigger() -> None:
    """Auto-Retrigger (2026-07-15): kommt während ein Run läuft eine 2. @gc:-Nachricht
    rein, sieht dieser Run sie nie (Prompt steht schon), aber seine eigene Antwort landet
    trotzdem NACH ihr im Faden (Appends sind streng chronologisch) — thread_status kippt
    auf for_owner, obwohl niemand die 2. Nachricht bearbeitet hat. Server muss das erkennen
    und automatisch einen Folge-Run starten, mit Inline-Hinweis im Prompt."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "slow-argv", "time.sleep(0.8)\n" + ARGV_ECHO)
        httpd, port = _serve(Path(tmp))
        try:
            code, _ = _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            check("retrigger: 202 accepted", code == 202)
            time.sleep(0.2)  # Run läuft garantiert (Fake-claude schläft 0.8s)
            code2, r2 = _post(port, "/api/gc-append",
                              {"kind": "ask", "text": "dritte frage waehrend des laufs",
                               "addr": {"id": "bbbbbbbbbbbb"}})
            check("retrigger: 2. Nachricht während des Laufs → 200", code2 == 200)

            def _item_replies() -> list[dict]:
                b = server.parse_board(Path(tmp).read_text())
                x = next(y for _s, _n, _c, y in server._all_items(b) if y.get("id") == "bbbbbbbbbbbb")
                return [e for e in x["thread"] if e["kind"] == "reply"]

            deadline = time.time() + 15
            while time.time() < deadline and len(_item_replies()) < 2:
                time.sleep(0.2)
            text = Path(tmp).read_text()
            board = server.parse_board(text)
            it = next(x for _s, _n, _c, x in server._all_items(board) if x.get("id") == "bbbbbbbbbbbb")
            asks = [e for e in it["thread"] if e["kind"] == "ask"]
            replies = [e for e in it["thread"] if e["kind"] == "reply"]
            check("retrigger: beide Nachrichten im Faden", len(asks) == 2)
            check("retrigger: zwei Antworten (Erst-Run + Auto-Retrigger)", len(replies) == 2)
            check("retrigger: Board bleibt verlustfrei", server.lost_total(text, board) == 0)

            second_reply_ref = replies[1]["text"].split("→ full reply: ")[-1].strip()
            sidecar = _sidecar_from_ref(second_reply_ref, Path(tmp))
            prompt = sidecar.read_text()
            check("retrigger: Prompt des Folge-Runs enthält die verpasste Nachricht",
                  "dritte frage waehrend des laufs" in prompt)
            check("retrigger: Prompt des Folge-Runs trägt den Inline-Hinweis",
                  "arrived before the previous reply was finished" in prompt)
            sidecar.unlink(missing_ok=True)
            first_reply_ref = replies[0]["text"].split("→ full reply: ")[-1].strip()
            _sidecar_from_ref(first_reply_ref, Path(tmp)).unlink(missing_ok=True)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_session_cut() -> None:
    """„Faden schließen" = Kontext-Schnitt: der nächste Run darf NICHT resumen.
    Der Schnitt gilt aber nur bis zur nächsten Antwort — sonst startete jeder
    Turn nach einem alten @gc-done: ewig eine neue Session (2026-07-14)."""
    ask, reply, done = {"kind": "ask"}, {"kind": "reply"}, {"kind": "done"}
    check("cut: leerer Faden resumed nicht (nichts zu resumen)", not gc_runner.session_cut([]))
    check("cut: offener Faden resumed", not gc_runner.session_cut([ask, reply, ask]))
    check("cut: done nach letzter Antwort ⇒ frische Session",
          gc_runner.session_cut([ask, reply, done, ask]))
    check("cut: done VOR der letzten Antwort ⇒ wieder resumen (neuer Abschnitt läuft)",
          not gc_runner.session_cut([ask, reply, done, ask, reply, ask]))


def test_interrupt_und_weiter() -> None:
    """„Unterbrechen & weiter" (2026-07-28): Nachricht anhängen + /api/gc-stop, und der
    Folge-Run muss AUTOMATISCH kommen, DIESELBE Session fortsetzen (--resume) und im Prompt
    erfahren, dass er absichtlich gestoppt wurde. Genau die Kette, die sonst nur im Kopf
    existiert: Kill → ⏹-Turn mit Session-Handle → _maybe_retrigger → Resume."""
    sid = "aaaaaaaa-0000-4000-8000-00000000abcd"
    # Fake-claude: meldet erst die Session (sonst hätte der Kill keinen Resume-Handle),
    # hängt dann — es sei denn, er wird resumt, dann antwortet er sofort.
    # ARGV_ECHO taugt hier nicht: sobald VOR dem Ergebnis ein Ereignis steht, ist stdout
    # kein einzelnes JSON mehr — dann greift der Strom-Zweig des Parsers, und der erkennt
    # das Ergebnis nur an "type":"result".
    echo = ('print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"permission_denials":[],"result":" ".join(sys.argv[1:]),'
            f'"session_id":"{sid}"}}))')
    body = (f'print(json.dumps({{"type":"system","subtype":"init","session_id":"{sid}"}}), flush=True)\n'
            "time.sleep(0 if '--resume' in sys.argv else 30)\n" + echo)
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    old_poll = gc_runner.POLL_EVERY
    gc_runner.POLL_EVERY = 1  # sonst wartet der Test bis zu 3s auf die Wache
    live_sidecars_before = set(gc_runner.SIDECAR_DIR.glob("bbbbbbbbbbbb-*.md"))
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "hangs-until-resumed", body)
        # Der Resume-Guard prüft seit 17.08.2026, ob das Transkript im Store DIESER Bahn
        # wirklich liegt (dangling Handles brachen Runs vor dem ersten Turn ab). Der
        # Fake-claude schreibt keins, also legen wir eines in einen Test-Store — sonst
        # prüfte der Test eine Situation, die es in echt gar nicht geben darf.
        store = Path(td) / "store" / "projects" / claude_identity.project_slug(gc_runner.GC_ROOT)
        store.mkdir(parents=True)
        (store / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        os.environ["GC_CLAUDE_STORE"] = str(Path(td) / "store")
        httpd, port = _serve(Path(tmp))
        try:
            _post(port, "/api/gc-run", {"id": "bbbbbbbbbbbb"})
            session_seen = False
            for _ in range(60):  # stoppen erst, wenn die Resume-ID wirklich im Strom angekommen ist
                time.sleep(0.1)
                with server.RUN_LOCK:
                    beat = server.BEATS.get("bbbbbbbbbbbb") or {}
                    if beat.get("stop_path") and beat.get("session_id") == sid:
                        session_seen = True
                        break
            check("interrupt: Session-Handle steht vor dem absichtlichen Stopp bereit", session_seen)
            code, _ = _post(port, "/api/gc-append",
                            {"kind": "ask", "text": "zusatzinfo mittendrin",
                             "addr": {"id": "bbbbbbbbbbbb"}})
            check("interrupt: Nachricht geht auch während des Laufs in den Faden", code == 200)
            code2, r2 = _post(port, "/api/gc-stop", {"id": "bbbbbbbbbbbb"})
            check("interrupt: Stopp angenommen", code2 == 202 and r2.get("ok"))

            def _replies() -> list[dict]:
                b = server.parse_board(Path(tmp).read_text())
                x = next(y for _s, _n, _c, y in server._all_items(b) if y.get("id") == "bbbbbbbbbbbb")
                return [e for e in x["thread"] if e["kind"] == "reply"]

            deadline = time.time() + 30
            while time.time() < deadline and len(_replies()) < 2:
                time.sleep(0.2)
            replies = _replies()
            check("interrupt: Abbruch steht als ⏹ im Faden (kein ❌-Absturz)",
                  bool(replies) and replies[0]["text"].startswith("⏹"))
            check("interrupt: Folge-Run läuft von allein an", len(replies) == 2)
            if len(replies) == 2:
                ref = replies[1]["text"].split("→ full reply: ")[-1].strip()
                sidecar = _sidecar_from_ref(ref, Path(tmp))
                echoed = sidecar.read_text()
                check("interrupt: Folge-Run setzt DIESELBE Session fort",
                      f"--resume {sid}" in echoed)
                check("interrupt: neue Info liegt dem Folge-Run vor",
                      "zusatzinfo mittendrin" in echoed)
                check("interrupt: Prompt sagt, dass der Stopp Absicht war",
                      "deliberately stopped" in echoed)
                check("interrupt: Antwort-Sidecar liegt neben dem Test-Board",
                      sidecar.parent == Path(tmp).parent / "gc-threads")
                sidecar.unlink(missing_ok=True)
                first = replies[0]["text"].split("→ full reply: ")[-1].strip()
                if first:
                    _sidecar_from_ref(first, Path(tmp)).unlink(missing_ok=True)
        finally:
            gc_runner.POLL_EVERY = old_poll
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            os.environ.pop("GC_CLAUDE_STORE", None)
            Path(tmp).unlink(missing_ok=True)
    check("interrupt: kein Sidecar leckt ins produktive inbox/gc-threads",
          set(gc_runner.SIDECAR_DIR.glob("bbbbbbbbbbbb-*.md")) == live_sidecars_before)


def test_gc_last_roundtrip_and_append() -> None:
    """@gc-last (Run-Meta: Kontextgröße + Zeitpunkt, 2026-07-16 Q3=A): parst,
    serialisiert verlustfrei, kollidiert mit keinem anderen @gc*-Tag; /api/gc-append
    nimmt es als optionales Feld im selben atomaren Write."""
    synth = SYNTH.replace("  @gc-session: sess-uuid-a · board-was-ist-loms",
                          "  @gc-session: sess-uuid-a · board-was-ist-loms\n  @gc-last: ~85k · 2026-07-16 14:32")
    sb = server.parse_board(synth)
    items = [it for _s, _n, _c, it in server._all_items(sb)]
    check("gc-last: geparst", items[0]["gc_last"] == "~85k · 2026-07-16 14:32")
    check("gc-last: nicht als thread/body verhackstückt",
          [e["kind"] for e in items[0]["thread"]] == ["ask"] and items[0]["body"] == ["Body-Zeile"])
    check("gc-last: lost-Guard = 0", server.lost_gc_last_lines(synth, sb) == 0
          and server.lost_total(synth, sb) == 0)
    check("gc-last: voller Roundtrip identisch", server.parse_board(server.serialize_board(sb)) == sb)

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        code, _r = _post(port, "/api/gc-append", {"addr": {"id": "aaaaaaaaaaaa"}, "kind": "reply",
                                                  "text": "antwort", "gc_last": "~42k · 2026-07-16 15:00"})
        check("gc-last: append 200", code == 200)
        check("gc-last: steht im Markdown", "@gc-last: ~42k · 2026-07-16 15:00" in Path(tmp).read_text())
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_gc_sessions_history() -> None:
    """@gc-sessions: (Plural, 10.08.) — Verlaufsliste ABGELÖSTER Resume-Pointer, damit
    ein Kontext-Schnitt die vorige Session nicht kommentarlos aus dem Board tilgt (das
    Transkript liegt weiter unter ~/.claude/projects/…/<uuid>.jsonl). Deckt: Roundtrip,
    Nachrücken beim Pointer-Wechsel, No-op bei --resume derselben Session, Kappung bei 10,
    Dedupe, den Lost-Guard und dass die geprüfte Ein-@gc-session:-Zeile-Invariante (eigener
    Marker, eigenes Feld) unberührt bleibt."""
    synth = SYNTH.replace(
        "  @gc-session: sess-uuid-a · board-was-ist-loms",
        "  @gc-session: sess-uuid-a · board-was-ist-loms\n"
        "  @gc-sessions: 11111111-1111-1111-1111-111111111111, 22222222-2222-2222-2222-222222222222")
    sb = server.parse_board(synth)
    items = [it for _s, _n, _c, it in server._all_items(sb)]
    check("gc-sessions: geparst", items[0]["sessions"] ==
          ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"])
    check("gc-sessions: nicht als thread/body verhackstückt",
          [e["kind"] for e in items[0]["thread"]] == ["ask"] and items[0]["body"] == ["Body-Zeile"])
    check("gc-sessions: lost-Guard = 0", server.lost_sessions_lines(synth, sb) == 0
          and server.lost_total(synth, sb) == 0)
    check("gc-sessions: voller Roundtrip identisch", server.parse_board(server.serialize_board(sb)) == sb)
    check("gc-sessions: Invariante — genau EINE @gc-session-Zeile bleibt bestehen",
          server.serialize_board(sb).count("\n  @gc-session: ") == 1)

    # Nachrücken: neuer Pointer löst alten ab → alte UUID landet vorn in sessions
    it = {"session": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa · board-x", "sessions": []}
    server._retire_session(it, it["session"], "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb · board-x")
    check("retire: alte UUID eingereiht", it["sessions"] == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])

    # Gleiche Session (--resume schreibt denselben Handle zurück) → kein Verlaufseintrag
    server._retire_session(it, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb · board-x",
                            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb · board-x")
    check("retire: --resume derselben Session erzeugt keinen Eintrag",
          it["sessions"] == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])

    # Kappung bei 10 + Dedupe: eine bereits vorhandene UUID rückt nach vorn statt sich zu verdoppeln
    it2 = {"session": "", "sessions": [f"{i:08d}-0000-0000-0000-000000000000" for i in range(10)]}
    server._retire_session(it2, "00000000-0000-0000-0000-000000000000 · x",
                           "ffffffff-ffff-ffff-ffff-ffffffffffff · y")
    check("retire: Liste bleibt bei 10 gekappt", len(it2["sessions"]) == 10)
    check("retire: bereits vorhandene UUID rückt vor statt zu duplizieren",
          it2["sessions"][0] == "00000000-0000-0000-0000-000000000000"
          and it2["sessions"].count("00000000-0000-0000-0000-000000000000") == 1)

    # Guard: doppelte @gc-sessions-Zeile blockt den Save statt still zu verlieren
    dbl = ("## T\n\n### Jetzt\n\n- [ ] X *(2026-07-10)*\n  @gc-sessions: aaa\n"
           "  @gc-sessions: bbb\n\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    check("guard: doppelte @gc-sessions → lost_sessions_lines>0",
          server.lost_sessions_lines(dbl, server.parse_board(dbl)) > 0)

    # Live über /api/gc-append: Pointer-Wechsel reiht die alte UUID ein, EINE @gc-session-Zeile bleibt
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH.replace(
        "  @gc-session: sess-uuid-a · board-was-ist-loms",
        "  @gc-session: 11111111-1111-1111-1111-111111111111 · board-was-ist-loms"))
    httpd, port = _serve(Path(tmp))
    try:
        code, _r = _post(port, "/api/gc-append", {"addr": {"id": "aaaaaaaaaaaa"}, "kind": "reply",
                                                  "text": "antwort", "session":
                                                  "22222222-2222-2222-2222-222222222222 · board-was-ist-loms"})
        check("gc-append: 200", code == 200)
        text = Path(tmp).read_text()
        check("gc-append: neuer Pointer steht",
              "@gc-session: 22222222-2222-2222-2222-222222222222 · board-was-ist-loms" in text)
        check("gc-append: alte UUID in @gc-sessions gelandet",
              "@gc-sessions: 11111111-1111-1111-1111-111111111111" in text)
        check("gc-append: genau EINE @gc-session-Zeile im Markdown", text.count("\n  @gc-session: ") == 1)
        check("gc-append: Board bleibt lost=0", server.lost_total(text, server.parse_board(text)) == 0)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


COCKPIT_SYNTH = """## Thema

### Jetzt

- [ ] Für Owner beantwortet *(2026-07-10)*
  @gc-id: c00000000001
  @gc: frage
  @gc-re: antwort
  @gc-last: ~40k · 2026-07-20 09:00

- [ ] GC offen *(2026-07-10)*
  @gc-id: c00000000002
  @gc: mach mal

- [ ] Überfällig !(2026-07-18) *(2026-07-10)*
  @gc-id: c00000000003

- [ ] Bald fällig !(2026-07-21) *(2026-07-10)*
  @gc-id: c00000000004

- [ ] Geschlossen, heute gelaufen *(2026-07-10)*
  @gc-id: c00000000005
  @gc: x
  @gc-re: y
  @gc-done:
  @gc-last: ~10k · 2026-07-20 08:00
- [x] Abgehakt, heute gelaufen *(2026-07-10)*
  @gc-id: c00000000006
  @gc-last: ~10k · 2026-07-20 08:30

### Wartet auf andere

- [ ] Frisches Wait *(2026-07-19)*
  @gc-id: c00000000007
  @wait: alex · !475 *(2026-07-19)*

- [ ] Verfallendes Wait *(2026-07-10)*
  @gc-id: c00000000008
  @wait: kollege *(2026-07-14)*

### Bald

### Geparkt

# Personen

# Notizen
"""


def test_cockpit_endpoint() -> None:
    """Cockpit: board_kpis zählt aus einem Fixture-Board deterministisch, und GET
    /api/cockpit liefert alles in einem Payload. Datum wird überall injiziert (kein
    date.today-Flake)."""
    from datetime import date as _date
    today = _date(2026, 7, 20)
    sb = server.parse_board(COCKPIT_SYNTH)
    check("cockpit: Fixture verlustfrei", server.lost_total(COCKPIT_SYNTH, sb) == 0)
    old_journal = server.JOURNAL_DIR
    with tempfile.TemporaryDirectory() as td:
        server.JOURNAL_DIR = Path(td) / "journal"
        server.JOURNAL_DIR.mkdir()
        # Journal-Meta eines Runs von heute für ein Item, dessen @gc-last noch fehlt (c…9),
        # + eins für ein schon via @gc-last gezähltes Item (c…1) → darf NICHT doppelt zählen.
        (server.JOURNAL_DIR / "run-c00000000009-20260720-101500-abcd.meta.json").write_text("{}")
        (server.JOURNAL_DIR / "run-c00000000001-20260720-113000-abcd.meta.json").write_text("{}")
        (server.JOURNAL_DIR / "run-c0000000000a-20260719-101500-abcd.meta.json").write_text("{}")  # gestern
        try:
            k = server.board_kpis(sb, today)
        finally:
            server.JOURNAL_DIR = old_journal
    check("cockpit: for_owner/for_gc", k["for_owner"] == 1 and k["for_gc"] == 1)
    check("cockpit: overdue/due_soon", k["overdue"] == 1 and k["due_soon"] == 1)
    check("cockpit: waits + verfallend", k["waits"] == 2 and k["waits_decaying"] == 1)

    # Überfällige Waits sind ein EIGENER Zustand (2026-07-22): seit dem Umbau bleiben sie
    # in „Wartet auf andere" liegen statt nach „Jetzt" zurückgeholt zu werden — die Kachel
    # und die Attention-Zeile sind damit die einzige Stelle, die sie noch hochspült.
    ow = server.parse_board(
        "## Dev\n\n### Jetzt\n\n### Wartet auf andere\n\n"
        "- [ ] Lange überfällig *(2026-07-01)*\n  @gc-id: c0000000000b\n  @wait: slim · !475 *(2026-07-10)*\n\n"
        "- [ ] Bald überfällig *(2026-07-01)*\n  @gc-id: c0000000000c\n  @wait: kim *(2026-07-14)*\n\n"
        "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    ok = server.board_kpis(ow, today)
    check("cockpit: überfälliges Wait zählt als overdue, nicht als decaying",
          ok["waits"] == 2 and ok["waits_overdue"] == 1 and ok["waits_decaying"] == 1)
    hints = [h["text"] for h in server.attention_hints(ow, today)]
    check("attention: überfälliges Wait steht beim Namen (mit Tagen + Wem)",
          any("Lange überfällig" in h and "10 days" in h and "slim · !475" in h for h in hints))
    check("attention: bald-überfälliges Wait taucht NICHT auf (nur Vorwarnung in der Kachel)",
          not any("Bald überfällig" in h for h in hints))
    # Runs heute: c…1 (gc_last, Journal-Duplikat egal) + c…5 offen + c…6 done + c…9 (nur Journal) = 4
    check("cockpit: runs_today zählt Items, done inklusive, dedupliziert", k["runs_today"] == 4)

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(COCKPIT_SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cockpit") as res:
            data = json.loads(res.read())
        check("cockpit: Endpoint 200 mit allen Zonen",
              set(data) == {"today", "kpis", "attention", "triage", "done_week",
                            "done_week_view", "wesen", "server_started", "server_stale",
                            "integrity"}
              and {"for_owner", "for_gc", "runs_today", "overdue", "due_soon",
                   "waits", "waits_decaying", "running", "queued"} <= set(data["kpis"])
              # "hungry" (Ritual überfällig) fehlte hier — der Zustand kam nach diesem Test
              # dazu und schlägt nur zu, wenn gerade wirklich ein Ritual überfällig ist.
              # Der Test war damit tageszeitabhängig rot (gefunden 06.08.).
              and data["wesen"]["state"] in ("healthy", "muede", "fat", "ache", "pochen",
                                             "stuffed", "smoke", "burst", "hungry"))
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)

    # Veralteter Serverprozess (Kontrakt-Diät 07.08.): mtime(server.py|gc_runner.py) gegen
    # SERVER_START. Beide Richtungen prüfen — sonst testet man nur, dass es nicht wirft.
    old_start = server.SERVER_START
    try:
        server.SERVER_START = time.time() + 3600      # Prozess jünger als jede Datei
        check("stale: frisch gestarteter Server meldet nichts", server.server_stale() == [])
        server.SERVER_START = 0                       # Prozess uralt
        check("stale: alter Server meldet beide Wachdateien",
              set(server.server_stale()) == set(server.STALE_WATCH))
    finally:
        server.SERVER_START = old_start


COCKPIT_SECTION_SYNTH = """## Thema

### Now

- [ ] Echtes Item *(2026-07-10)*
  @gc-id: e00000000001
  @gc: mach was

### Next

### Backlog

# Cockpit

- [ ] AI-News *(2026-07-19)*
  action:ai-news
  ···
  Alte Mission.
  @gc-id: ac0000000001
  @gc: ▶ AI-News ausführen
  @gc-re: 5 News gefunden …
  @gc-last: ~30k · 2026-07-19 09:00

- [ ] Wartende Action *(2026-07-19)*
  action:wartend
  @gc-id: ac0000000002
  @gc: ▶ Wartende Action ausführen

# To discuss

# Notes
"""


def test_cockpit_section() -> None:
    """E3-Fundament: die Sektion '# Cockpit' (Quick-Action-Pseudo-Items) parst
    verlustfrei, round-trippt wortgleich, taucht in keiner Themen-/Personen-Liste
    auf, wird von KPIs (außer runs_today), Run-all und Sweep ignoriert — und ein
    Alt-Client-Save ohne cockpit-Key darf die Sektion nicht vernichten."""
    from datetime import date as _date
    sb = server.parse_board(COCKPIT_SECTION_SYNTH)
    check("cockpit-md: 2 Pseudo-Items geparst", len(sb["cockpit"]) == 2
          and sb["cockpit"][0]["title"] == "AI-News"
          and sb["cockpit"][0]["body"] == ["action:ai-news", "···", "Alte Mission."]
          and [e["kind"] for e in sb["cockpit"][0]["thread"]] == ["ask", "reply"]
          and sb["cockpit"][0]["gc_last"] == "~30k · 2026-07-19 09:00")
    check("cockpit-md: lost-Guards = 0", server.lost_total(COCKPIT_SECTION_SYNTH, sb) == 0)
    check("cockpit-md: Roundtrip wortgleich",
          server.serialize_board(sb) == COCKPIT_SECTION_SYNTH)
    check("cockpit-md: nicht in themes/persons",
          all(it["title"] != "AI-News" for th in sb["themes"] for c in server.theme_cols(th)
              for it in th["cols"][c]) and not sb["persons"])
    k = server.board_kpis(sb, _date(2026, 7, 19))
    check("cockpit-md: KPIs ohne Pseudo-Items (for_owner 0, for_gc 1 echtes), runs_today zählt sie",
          k["for_owner"] == 0 and k["for_gc"] == 1 and k["runs_today"] == 1)

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(COCKPIT_SECTION_SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        # Run-all darf die wartende Cockpit-Action NICHT anfassen (auth-Bypass-Schutz) —
        # nur das echte for_gc-Item wird gequeued. Fake-claude nötig, sonst realer Spawn.
        with tempfile.TemporaryDirectory() as td:
            server.CLAUDE_BIN = _fake_claude(Path(td), "ok", OK_JSON)
            try:
                code, r = _post(port, "/api/gc-run-all", {})
                check("cockpit-md: Run-all queued nur echte Items",
                      code == 202 and r.get("queued") == ["e00000000001"])
                deadline = time.time() + 10
                while time.time() < deadline and json.load(urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/etag")).get("running"):
                    time.sleep(0.1)
            finally:
                server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
        # gc-pending enthält die Cockpit-Action weiterhin (Journal-Recovery braucht das)
        gp = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/gc-pending"))
        check("cockpit-md: gc-pending enthält Cockpit-Action",
              any(p["addr"]["id"] == "ac0000000002" for p in gp["pending"]))
        # Alt-Client-Simulation: Whole-Board-Save OHNE cockpit-Key → Sektion überlebt
        rb = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
        old_board = dict(rb["board"])
        old_board.pop("cockpit", None)
        code, _r = _post(port, "/api/board", {"board": old_board, "baseEtag": rb["etag"]})
        text = Path(tmp).read_text()
        check("cockpit-md: Alt-Client-Save vernichtet Sektion nicht",
              code == 200 and "# Cockpit" in text and "action:ai-news" in text
              and "@gc-id: ac0000000002" in text)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


STAGING_SECTION_SYNTH = """# Staging

- [ ] 📥 Review bundle — 3 suggestions *(2026-07-30)*
  Suggestions from today's review. [src: weekly-review 2026-07-30]
  Sheet: inbox/staging/2026-07-30-weekly-review.html
  @gc-id: 5a0000000001
  @gc: 1 ja, 2 weg
  - [ ] Kandidat A → Ziel: Dev (Board)/Jetzt
  - [x] Kandidat B → Ziel: Inbox/Bald

## Thema

### Now

- [ ] Echtes Item *(2026-07-10)*
  @gc-id: e00000000001

### Next

### Backlog

# To discuss

# Notes
"""


def test_staging_section() -> None:
    """Vorschlags-Staging (Faden e5bb9b10d7eb, 30.07.): die Sektion '# Staging' steht
    ÜBER der Matrix, parst verlustfrei, round-trippt wortgleich, taucht in keiner
    Themen-/Personen-Liste auf — und ein Alt-Client-Save ohne staging-Key darf die
    Sektion nicht vernichten (dieselbe Falle wie bei Cockpit)."""
    sb = server.parse_board(STAGING_SECTION_SYNTH)
    it = sb["staging"][0]
    check("staging-md: Bündel geparst", len(sb["staging"]) == 1
          and it["title"].startswith("📥 Review bundle")
          and it["body"][0].endswith("[src: weekly-review 2026-07-30]")
          and [s["done"] for s in it["subs"]] == [False, True]
          and [e["kind"] for e in it["thread"]] == ["ask"])
    check("staging-md: lost-Guards = 0", server.lost_total(STAGING_SECTION_SYNTH, sb) == 0)
    check("staging-md: Roundtrip wortgleich",
          server.serialize_board(sb) == STAGING_SECTION_SYNTH)
    check("staging-md: nicht in themes/persons",
          all(i["title"] != it["title"] for th in sb["themes"] for c in server.theme_cols(th)
              for i in th["cols"][c]) and not sb["persons"])
    check("staging-md: über der Matrix serialisiert",
          server.serialize_board(sb).index("# Staging")
          < server.serialize_board(sb).index("## Thema"))
    # Malformed: eine Checkbox-Zeile, die der Parser NICHT als Item/Sub fasst
    # (Attribut-Einrückung ohne vorangehendes Item) muss den Save blocken.
    broken = STAGING_SECTION_SYNTH.replace("# Staging\n\n- [ ] 📥",
                                           "# Staging\n\n   - [ ] verwaist\n\n- [ ] 📥")
    check("staging-md: verwaiste Checkbox blockt (lost > 0)",
          server.lost_total(broken, server.parse_board(broken)) > 0)

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(STAGING_SECTION_SYNTH)
    httpd, port = _serve(Path(tmp))
    try:
        rb = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
        check("staging-md: /api/board liefert staging", len(rb["board"]["staging"]) == 1)
        old_board = dict(rb["board"])
        old_board.pop("staging", None)
        code, _r = _post(port, "/api/board", {"board": old_board, "baseEtag": rb["etag"]})
        text = Path(tmp).read_text()
        check("staging-md: Alt-Client-Save vernichtet Sektion nicht",
              code == 200 and "# Staging" in text and "@gc-id: 5a0000000001" in text
              and "Kandidat A → Ziel: Dev (Board)/Jetzt" in text)
    finally:
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)


def test_testrig_endpoints() -> None:
    """The Superboard test-rig button. The costliest failure here is silent: a
    truncated wait page loads no script and the tab just stays blank — that's why
    the first case checks Content-Length against the actual BYTES (with umlauts
    that isn't the same number as the character count)."""

    class _FakeProc:
        returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    fake_script = Path(tmp).with_suffix(".sh")
    fake_script.write_text("#!/bin/sh\n")
    httpd, port = _serve(Path(tmp))
    old_script, old_listen = server.testrig_script, server.testrig_listening
    old_popen = server.subprocess.Popen
    calls: list[list[str]] = []
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/testrig?mode=fresh") as r:
            body, clen = r.read(), int(r.headers["Content-Length"])
        check("testrig: wait page arrives complete", len(body) == clen)
        check("testrig: wait page ends in its script", body.rstrip().endswith(b"</script>"))
        check("testrig: wait page polls /api/testrig", b"/api/testrig" in body)

        code, _ = _post(port, "/api/testrig", {"mode": "rm -rf /"})
        check("testrig: unknown mode -> 400", code == 400)

        server.testrig_script = lambda: None
        code, j = _post(port, "/api/testrig", {"mode": "fresh"})
        check("testrig: no checkout -> 404 instead of a silent failure",
              code == 404 and "testrig.sh" in j.get("error", ""))

        server.testrig_script = lambda: fake_script
        server.testrig_listening = lambda: False
        server.subprocess.Popen = lambda cmd, **kw: (calls.append(list(cmd)) or _FakeProc())
        code, j = _post(port, "/api/testrig", {"mode": "fresh"})
        check("testrig: fresh -> 202", code == 202 and j.get("starting") is True)
        check("testrig: starts exactly 'sh <script> fresh'",
              calls == [["sh", str(fake_script), "fresh"]])

        server.testrig_listening = lambda: True
        code, j = _post(port, "/api/testrig", {"mode": "up"})
        check("testrig: 'up' on an already-running rig starts no second process",
              code == 200 and j.get("running") is True and len(calls) == 1)
    finally:
        server.testrig_script, server.testrig_listening = old_script, old_listen
        server.subprocess.Popen = old_popen
        server.TESTRIG_STATE.update({"proc": None, "error": ""})
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)
        fake_script.unlink(missing_ok=True)


def test_action_run_endpoint() -> None:
    """E3-Durchstich: POST /api/action-run legt das Pseudo-Item in '# Cockpit' an
    (Marker + Mission im Body, kurzer Klick-Turn), startet den Agenten (Fake), die
    Antwort landet im Aktions-Faden; zweiter Klick reused das Item statt zu doppeln;
    unbekannte Action → 404."""
    actions = {"actions": [{"key": "test-act", "label": "Test-Action", "icon": "🧪",
                            "auth": False, "prompt": "Mach die Testsache."}]}
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    old_actions = server.ACTIONS_FILE
    with tempfile.TemporaryDirectory() as td:
        server.ACTIONS_FILE = Path(td) / "actions.json"
        server.ACTIONS_FILE.write_text(json.dumps(actions))
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            aj = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/actions"))
            check("action: GET /api/actions liefert Defs ohne Prompt",
                  aj["actions"] == [{"key": "test-act", "label": "Test-Action", "icon": "🧪", "auth": False}])
            # "status" (Stand-Vermerk, den der Action-Agent selbst zurückschreibt) fliesst
            # durch, wenn gesetzt — ohne Feld bleibt die Payload exakt wie oben.
            server.ACTIONS_FILE.write_text(json.dumps({"actions": [
                {**actions["actions"][0], "status": "Stand 2026-07-27: 0 offen"}]}))
            aj2 = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/actions"))
            check("action: status wird durchgereicht, Prompt weiterhin nicht",
                  aj2["actions"][0].get("status") == "Stand 2026-07-27: 0 offen"
                  and "prompt" not in aj2["actions"][0])
            # run_endpoint muss durchfliessen: fehlt es in der Payload, faellt die UI
            # still auf /api/action-run zurueck und der Sonderpfad einer Action mit
            # eigenem Trigger-Thread bleibt tot — genau der Bug vom 11.08.
            # Fremde URLs werden dabei geschluckt, damit kein fetch() nach draussen geht.
            server.ACTIONS_FILE.write_text(json.dumps({"actions": [
                {**actions["actions"][0], "run_endpoint": "/api/custom-pull"},
                {**actions["actions"][0], "key": "boese", "run_endpoint": "https://evil.example/x"}]}))
            aj3 = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/actions"))
            check("action: run_endpoint wird durchgereicht (sonst startet ▶ den falschen Pfad)",
                  aj3["actions"][0].get("run_endpoint") == "/api/custom-pull")
            check("action: run_endpoint ausserhalb /api/ wird verworfen",
                  "run_endpoint" not in aj3["actions"][1])
            server.ACTIONS_FILE.write_text(json.dumps(actions))
            code, r = _post(port, "/api/action-run", {"key": "test-act"})
            check("action: 202 + id", code == 202 and r.get("ok") and r.get("id"))
            deadline = time.time() + 15
            while time.time() < deadline and "@gc-re:" not in Path(tmp).read_text().split("# Cockpit")[-1]:
                time.sleep(0.2)
            text = Path(tmp).read_text()
            cockpit_part = text.split("# Cockpit")[-1].split("# Personen")[0]
            check("action: Pseudo-Item mit Marker+Mission+Klick-Turn",
                  "- [ ] Test-Action" in cockpit_part and "action:test-act" in cockpit_part
                  and "Mach die Testsache." in cockpit_part
                  and "@gc: ▶ Run Test-Action" in cockpit_part)
            check("action: Antwort im Aktions-Faden", "@gc-re: testantwort vom agenten" in cockpit_part)
            check("action: Roundtrip nach Run sauber",
                  server.lost_total(text, server.parse_board(text)) == 0)
            # zweiter Klick: Item wird wiederverwendet (1x '- [ ] Test-Action'), neuer Turn
            code2, _r2 = _post(port, "/api/action-run", {"key": "test-act"})
            deadline = time.time() + 15
            # NUR die Cockpit-Sektion zählen — SYNTH bringt selbst @gc-re-Turns mit;
            # ein Datei-weiter Count bräche sofort ab und der Shutdown killte den Run
            # mitten im Append (hinterließ einen RUNNING-Zombie → Compact-Test rot).
            while time.time() < deadline and Path(tmp).read_text().split("# Cockpit")[-1].count("@gc-re:") < 2:
                time.sleep(0.2)
            deadline = time.time() + 5
            while time.time() < deadline and json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/etag")).get("running"):
                time.sleep(0.1)
            text2 = Path(tmp).read_text()
            check("action: 2. Klick reused Item", code2 == 202
                  and text2.count("- [ ] Test-Action") == 1
                  and text2.count("@gc: ▶ Run Test-Action") == 2)
            code3, _ = _post(port, "/api/action-run", {"key": "gibtsnicht"})
            check("action: unbekannte Action → 404", code3 == 404)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            server.ACTIONS_FILE = old_actions
            Path(tmp).unlink(missing_ok=True)


def test_action_run_fresh_session() -> None:
    """▶ = neue Runde = frische Session (2026-08-06, Item 632bd6a8a6d5).
    Der Klick löscht den Resume-Pointer am Pseudo-Item, der Run startet also OHNE
    --resume — auch beim zweiten Klick, obwohl der erste eine session_id abgelegt hat.
    Der FADEN bleibt: beide Klick-Turns und beide Antworten stehen weiter im Item.
    (Weitertippen im Faden resumt unverändert — das läuft über /api/gc-run, nicht hier.)"""
    actions = {"actions": [{"key": "fresh-act", "label": "Fresh-Action", "icon": "🧪",
                            "auth": False, "prompt": "Mach die Testsache."}]}
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    old_actions = server.ACTIONS_FILE
    with tempfile.TemporaryDirectory() as td:
        server.ACTIONS_FILE = Path(td) / "actions.json"
        server.ACTIONS_FILE.write_text(json.dumps(actions))
        argv_log = Path(td) / "argv.jsonl"
        # Fake-claude, das seine eigene Kommandozeile protokolliert — nur so ist belegbar,
        # dass --resume WIRKLICH nicht mitging (board.md zeigt hinterher wieder eine
        # session_id, weil der Lauf seine neue zurückschreibt; das allein beweist nichts).
        server.CLAUDE_BIN = _fake_claude(Path(td), "argvlog", (
            f'open({str(argv_log)!r}, "a").write(json.dumps(sys.argv) + "\\n")\n' + OK_JSON))
        httpd, port = _serve(Path(tmp))
        try:
            for n in (1, 2):
                code, _ = _post(port, "/api/action-run", {"key": "fresh-act"})
                check(f"fresh: Klick {n} → 202", code == 202)
                deadline = time.time() + 15
                while time.time() < deadline and Path(tmp).read_text().split(
                        "# Cockpit")[-1].count("@gc-re:") < n:
                    time.sleep(0.2)
                deadline = time.time() + 5
                while time.time() < deadline and json.load(urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/etag")).get("running"):
                    time.sleep(0.1)
            calls = [json.loads(l) for l in argv_log.read_text().splitlines() if l.strip()]
            check("fresh: beide Runs gestartet", len(calls) == 2)
            check("fresh: kein --resume bei ▶", all("--resume" not in c for c in calls))
            cockpit = Path(tmp).read_text().split("# Cockpit")[-1].split("# Personen")[0]
            check("fresh: Faden bleibt vollständig",
                  cockpit.count("@gc: ▶ Run Fresh-Action") == 2
                  and cockpit.count("@gc-re: testantwort vom agenten") == 2)
            check("fresh: neue session_id wird wieder abgelegt",
                  "@gc-session: fa4e5e55-0000-4000-8000-00000000e2e1" in cockpit)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            server.ACTIONS_FILE = old_actions
            Path(tmp).unlink(missing_ok=True)


def test_chat_send() -> None:
    """E5: /api/chat-send legt das Tages-Chat-Item in '# Cockpit' an (chat:<datum>-Marker,
    Mission im Body), hängt den Turn an und startet den Agenten; zweiter Send am selben
    Tag reused das Item; leerer Text → 400."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/chat-send", {"text": "leg mal ein todo für X an"})
            check("chat: 202 + id", code == 202 and r.get("ok"))
            today = time.strftime("%Y-%m-%d")
            deadline = time.time() + 15
            while time.time() < deadline and "@gc-re:" not in Path(tmp).read_text().split("# Cockpit")[-1]:
                time.sleep(0.2)
            text = Path(tmp).read_text()
            cp = text.split("# Cockpit")[-1].split("# Personen")[0]
            check("chat: Tages-Item mit Marker + Mission",
                  f"- [ ] Chat {today}" in cp and f"chat:{today}" in cp and "daily cockpit chat" in cp)
            check("chat: Turn + Antwort im Faden",
                  "@gc: leg mal ein todo für X an" in cp and "@gc-re: testantwort vom agenten" in cp)
            code2, _ = _post(port, "/api/chat-send", {"text": "noch eins"})
            deadline = time.time() + 15
            while time.time() < deadline and json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/etag")).get("running"):
                time.sleep(0.1)
            text2 = Path(tmp).read_text()
            check("chat: 2. Send reused Tages-Item", code2 == 202
                  and text2.count(f"- [ ] Chat {today}") == 1)
            code3, _ = _post(port, "/api/chat-send", {"text": "  "})
            check("chat: leer → 400", code3 == 400)
            check("chat: Roundtrip sauber",
                  server.lost_total(text2, server.parse_board(text2)) == 0)
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_triage() -> None:
    """E7: JSON-Extraktion (Zäune/Umtext), Prompt-Inhalt (offene Items, keine done/
    Cockpit-Items), Slot-Logik (Selbstheilung nach Sleep) und der volle Lauf über
    /api/triage-run mit Fake-Opus → triage-latest.json + /api/cockpit-Zone."""
    from datetime import date as _date
    check("triage: JSON pur", server._extract_json('{"a": 1}') == {"a": 1})
    check("triage: JSON im Zaun", server._extract_json('bla\n```json\n{"a": 1}\n```\nnachtext') == {"a": 1})
    check("triage: JSON mit Umtext", server._extract_json('Hier: {"a": {"b": 2}} fertig.') == {"a": {"b": 2}})
    broken = ('{"quick": [{"id": "e00000000001", "note": "short"}], "stale": [], '
              '"deep": [{"id": "e00000000002", "note": "deep"}], "footnotes": ["x"]')
    check("triage: missing closing bracket is repaired",
          server._extract_json(broken)["deep"][0]["id"] == "e00000000002")
    check("triage: truncated string is repaired",
          server._extract_json('{"quick": [{"id": "e1", "note": "cut off')["quick"][0]["id"] == "e1")
    server.TRIAGE_STATE.update({"running": False, "error": "", "failed_slot": "2026-07-21T08:30:00"})
    check("triage: failed slot is not retried automatically",
          server._triage_due("2026-07-21T08:30:00", "") is False)
    check("triage: next slot is eligible again",
          server._triage_due("2026-07-21T12:30:00", "") is True)
    server.TRIAGE_STATE["failed_slot"] = ""

    sb = server.parse_board(COCKPIT_SECTION_SYNTH)
    prompt = server._triage_prompt(sb, _date(2026, 7, 21))
    check("triage: Prompt trägt offenes Item mit id + Ort",
          "id=e00000000001 [Thema/Jetzt] Echtes Item" in prompt and "open 11d" in prompt)
    check("triage: Prompt ohne Cockpit-Pseudo-Items", "AI-News" not in prompt)

    lt = time.strptime("2026-07-21 07:15", "%Y-%m-%d %H:%M")
    check("triage: vor 08:30 kein Slot", server._last_slot(lt) == "")
    lt2 = time.strptime("2026-07-21 09:00", "%Y-%m-%d %H:%M")
    check("triage: 09:00 → 08:30-Slot", server._last_slot(lt2) == "2026-07-21T08:30:00")
    lt3 = time.strptime("2026-07-21 23:59", "%Y-%m-%d %H:%M")
    check("triage: abends → 12:30-Slot", server._last_slot(lt3) == "2026-07-21T12:30:00")

    triage_json = json.dumps({"groups": {"quick": [{"id": "e00000000001", "note": "Nur kurz abnicken."}],
                                         "stale": [], "deep": []},
                              "footnotes": ["Dev-Spalte läuft voll."]})
    envelope = ('print(json.dumps({"result": ' + json.dumps(triage_json)
                + ', "session_id": "fa4e5e55-0000-4000-8000-0000000000tr", "permission_denials": [],'
                  ' "subtype": "success", "is_error": False}))')
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(COCKPIT_SECTION_SYNTH)
    old_file = server.TRIAGE_FILE
    with tempfile.TemporaryDirectory() as td:
        server.TRIAGE_FILE = Path(td) / "triage-latest.json"
        server.CLAUDE_BIN = _fake_claude(Path(td), "opus-fake", envelope)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/triage-run", {})
            check("triage: Run 202", code == 202 and r.get("ok"))
            deadline = time.time() + 10
            while time.time() < deadline and not server.TRIAGE_FILE.is_file():
                time.sleep(0.1)
            deadline = time.time() + 5
            while time.time() < deadline and server.TRIAGE_STATE["running"]:
                time.sleep(0.05)
            data = json.loads(server.TRIAGE_FILE.read_text())
            check("triage: Datei mit Gruppen + Fußnote + Stempel",
                  data["groups"]["quick"][0]["id"] == "e00000000001"
                  and data["footnotes"] == ["Dev-Spalte läuft voll."]
                  and data["generated"].startswith("20") and data["model"] == "opus")
            cp = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cockpit"))
            check("triage: /api/cockpit liefert die Zone",
                  cp["triage"]["present"] and cp["triage"]["groups"]["quick"][0]["note"] == "Nur kurz abnicken."
                  and cp["triage"]["running"] is False and cp["triage"]["error"] == "")
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            server.TRIAGE_FILE = old_file
            server.TRIAGE_STATE.update({"running": False, "error": "", "failed_slot": ""})
            Path(tmp).unlink(missing_ok=True)


def test_triage_snooze() -> None:
    """Zurückstellen einer Triage-Zeile (+1h/+1d, 2026-08-16): Endpoint schreibt,
    /api/cockpit liefert die Liste mit, Abgelaufenes und Müll fallen beim Lesen raus.
    board.md wird dabei NIE angefasst — der Snooze ist reiner Anzeigezustand."""
    from datetime import datetime as _dt, timedelta as _td
    old_file = server.TRIAGE_SNOOZE_FILE
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(COCKPIT_SECTION_SYNTH)
    before = Path(tmp).read_text()
    with tempfile.TemporaryDirectory() as td:
        server.TRIAGE_SNOOZE_FILE = Path(td) / "triage-snooze.json"
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/triage-snooze", {"id": "e00000000001", "hours": 1})
            check("snooze: +1h ok", code == 200 and r["until"] > _dt.now().isoformat(timespec="minutes"))
            code, _ = _post(port, "/api/triage-snooze", {"id": "e00000000002", "hours": 24})
            check("snooze: +1d ok", code == 200)
            code, _ = _post(port, "/api/triage-snooze", {"id": "e00000000003", "hours": 3})
            check("snooze: fremde Stufe → 400", code == 400)
            code, _ = _post(port, "/api/triage-snooze", {"id": "", "hours": 1})
            check("snooze: ohne id → 400", code == 400)
            # Abgelaufenes und Unlesbares fallen beim Lesen raus, ohne Aufräumjob.
            data = json.loads(server.TRIAGE_SNOOZE_FILE.read_text())
            data["e00000000009"] = (_dt.now() - _td(hours=3)).isoformat(timespec="minutes")
            data["e00000000010"] = "kein Datum"
            server.TRIAGE_SNOOZE_FILE.write_text(json.dumps(data))
            cp = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cockpit"))
            sn = cp["triage"]["snoozed"]
            check("snooze: /api/cockpit zeigt nur Gültiges",
                  set(sn) == {"e00000000001", "e00000000002"})
            check("snooze: board.md unberührt", Path(tmp).read_text() == before)
        finally:
            httpd.shutdown()
            server.TRIAGE_SNOOZE_FILE = old_file
            Path(tmp).unlink(missing_ok=True)


def test_restart_drain() -> None:
    """Neustart-Drain: solange restart-server.sh auf das Auslaufen der Runs wartet, darf
    launch_gc_run KEINEN neuen Run mehr starten — sonst verhungert der Wächter auf einem
    normalen Board-Tag und das Board bleibt auf der alten Version. Ein verwaistes Lock
    (Wächter tot) darf umgekehrt nicht ewig blockieren."""
    old_lock = server.RESTART_LOCK
    try:
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "board-restart.lock"
            server.RESTART_LOCK = lock
            check("drain: kein Lock → normal", server.restart_draining() is False)

            lock.mkdir()
            check("drain: frisches Lock → drainet", server.restart_draining() is True)

            sem = threading.Semaphore(1)
            sem.acquire()
            pending = {"addr": {"id": "drainxx"}}
            started = server.launch_gc_run(pending, "http://127.0.0.1:1", "claude", 60,
                                           semaphore=sem)
            check("drain: launch_gc_run startet nicht", started is False)
            check("drain: Semaphor wird trotzdem freigegeben", sem.acquire(blocking=False))
            check("drain: Item landet nicht in RUNNING", "drainxx" not in server.RUNNING)

            os.utime(lock, (0, time.time() - server.RESTART_DRAIN_MAX - 60))
            check("drain: verwaistes Lock blockiert nicht ewig",
                  server.restart_draining() is False)
    finally:
        server.RESTART_LOCK = old_lock


def test_plain_binary_cannot_be_switched_by_parent_env() -> None:
    old_account = os.environ.get("GC_RUNNER_CLAUDE")
    old_binary = os.environ.get("GC_RUNNER_CLAUDE_BIN")
    old_constant = server.CLAUDE_BIN
    try:
        os.environ["GC_RUNNER_CLAUDE"] = "alternate-claude"
        os.environ["GC_RUNNER_CLAUDE_BIN"] = "alternate-claude"
        server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
        check("identity: inherited binary/account switches ignored",
              server.claude_binary() == server.DEFAULT_CLAUDE_BIN)
    finally:
        server.CLAUDE_BIN = old_constant
        for key, value in (("GC_RUNNER_CLAUDE", old_account),
                           ("GC_RUNNER_CLAUDE_BIN", old_binary)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_wesen_status() -> None:
    """Wesen-Heuristik (Atari T5; Schwellen kalibriert 21.07. nach Blatt): deterministisch,
    Vorrang burst > stuffed > ache > fat > healthy. Synthetische Boards je Zustand; Archiv
    wird auf einen nicht existenten Pfad gelenkt, damit nur die Board-Daten zählen."""
    from datetime import date as _date
    today = _date(2026, 7, 21)
    noarch = Path("/nonexistent")

    def board_md(items_jetzt: str, extra: str = "") -> dict:
        return server.parse_board(f"## Thema\n\n### Jetzt\n\n{items_jetzt}\n"
                                  f"### Bald\n\n{extra}\n### Geparkt\n")

    def jetzt(n: int, iso: str) -> str:
        return "\n".join(f"- [ ] Item {i} *({iso})*" for i in range(n))

    # burst: BEIDE Beine hart — >15 in Jetzt UND ältestes >14d
    many = jetzt(16, "2026-07-01")  # 20 Tage alt
    w = server.wesen_status(board_md(many), today, noarch)
    check("wesen: Menge UND Alter hart → burst",
          w["state"] == "burst" and "NOW 16/15" in w["why"])
    # smoke (30.07.): dieselbe volle Last, aber der produktive Abfluss trägt —
    # „viel los, aber auch viel gemacht". 9 Erledigungen in 7D, kein Zufluss.
    w = server.wesen_status(board_md(many + "\n" + "\n".join(
        f"- [x] Weg {i} *(2026-07-20)*" for i in range(9))), today, noarch)
    check("wesen: volle Last, aber Abfluss ≥ 8 und ≥ Zufluss → smoke",
          w["state"] == "smoke" and "OUT 9" in w["why"])
    # Gegenprobe: 7 Erledigungen reichen nicht (WESEN_RAUCHT_OUT = 8) → weiter burst.
    w = server.wesen_status(board_md(many + "\n" + "\n".join(
        f"- [x] Weg {i} *(2026-07-20)*" for i in range(7))), today, noarch)
    check("wesen: Abfluss unter der Rauch-Schwelle → bleibt burst", w["state"] == "burst")
    # Gegenprobe 2: Abfluss über der Schwelle, aber der Zufluss hängt ihn klar ab
    # (9 raus vs. 20 rein, Verhältnis unter WESEN_RAUCHT_RATIO) → Untergehen, kein Rauchen.
    flut = "\n".join(f"- [ ] Flut {i} *(2026-07-20)*" for i in range(20))
    w = server.wesen_status(board_md(many + "\n" + flut + "\n" + "\n".join(
        f"- [x] Weg {i} *(2026-07-20)*" for i in range(9))), today, noarch)
    check("wesen: Zufluss hängt den Abfluss ab → burst statt smoke", w["state"] == "burst")
    # ache: nur die Menge ist hart drüber → Vorstufe „fast gekippt". Datum bewusst
    # ausserhalb des 7-Tage-Zuflussfensters, sonst greift vorher stuffed.
    w = server.wesen_status(board_md(jetzt(16, "2026-07-12")), today, noarch)
    check("wesen: nur Menge hart → ache", w["state"] == "ache")
    # 13 Items / 13 Tage: bis 07.08. „beide Beine weich → ache". Mit dem graduierten Score
    # (07.08.) landet genau dieser Fall eine Stufe tiefer auf der NEUEN Vorstufe MÜDE —
    # das ist der beabsichtigte Effekt, nicht ein Regress: kein Item ist überaltert, die
    # Menge liegt unter der Kappe, das ist noch keine Kopfschmerz-Lage.
    w = server.wesen_status(board_md(jetzt(13, "2026-07-08")), today, noarch)
    check("wesen: knapp unter beiden Schwellen → muede (Vorstufe)", w["state"] == "muede")
    # healthy: knapp unter beiden Weich-Schwellen (10 Items, 11 Tage)
    w = server.wesen_status(board_md(jetzt(10, "2026-07-10")), today, noarch)
    check("wesen: unter allen Schwellen → healthy", w["state"] == "healthy")
    # stuffed: 4 frische rein, nichts erledigt
    fresh = "\n".join(f"- [ ] Neu {i} *(2026-07-20)*" for i in range(4))
    w = server.wesen_status(board_md(fresh), today, noarch)
    check("wesen: Zufluss 4 ohne Abfluss → stuffed", w["state"] == "stuffed")
    # fat: ★-Item liegt >3d, während anderes erledigt wurde (done heute = diese Woche)
    fatmd = ("- [ ] **Wichtig** *(2026-07-10)*\n- [x] Erledigt *(2026-07-21)*")
    w = server.wesen_status(board_md(fatmd), today, noarch)
    check("wesen: ★ liegt + anderes erledigt → fat", w["state"] == "fat" and "★" in w["why"])
    # healthy: wenig los, nichts verletzt
    w = server.wesen_status(board_md("- [ ] Eins *(2026-07-20)*"), today, noarch)
    check("wesen: ruhiges Board → healthy", w["state"] == "healthy")
    # Dev-Themen zählen nicht (Scope = To-dos-Board)
    dev = server.parse_board("## Dev (Work)\n\n### Jetzt\n\n" + many + "\n### Bald\n\n### Geparkt\n")
    check("wesen: Dev-Themen außerhalb des Scopes",
          server.wesen_status(dev, today, noarch)["state"] == "healthy")


def test_wesen_graduiert_und_gedaechtnis() -> None:
    """Graduierter Last-Score + Kurz-Gedächtnis (07.08., Blatt „Wesen-Logik", Q1=B).

    Der Anlass, gegen den hier getestet wird: 13 Tage Historie zeigten 12 Tage nicht-healthy,
    weil EIN seit dem 15.07. liegendes Item („Enablement-Workstream", wartet strukturell auf
    einen Kollegen im Urlaub) das binäre Alters-Bein dauerhaft hart hielt. Ein einzelner
    Uralt-Brocken muss spürbar bleiben, darf aber nicht mehr allein eskalieren."""
    from datetime import date as _date, timedelta
    today = _date(2026, 8, 7)
    noarch = Path("/nonexistent")

    def board_md(items: str) -> dict:
        return server.parse_board(f"## Thema\n\n### Jetzt\n\n{items}\n### Bald\n\n### Geparkt\n")

    def frisch(n: int) -> str:  # frisch genug, dass weder Alter noch Zufluss-Saldo greifen
        return "\n".join(f"- [ ] Frisch {i} *(2026-07-25)*" for i in range(n))

    def uralt(n: int) -> str:
        return "\n".join(f"- [ ] Uralt {i} *(2026-07-15)*" for i in range(n))

    # (a) EIN uraltes Item bei ruhigem Board eskaliert nicht mehr — der Kern-Fix.
    einer = server.wesen_status(board_md(frisch(3) + "\n" + uralt(1)), today, noarch)
    check("wesen: ein einzelnes uraltes Item zieht nicht mehr allein in den Alarm",
          einer["state"] in ("healthy", "muede") and einer["alter"] > 0.5)
    # (b) …aber vier davon schon. Die ANZAHL trägt das Alters-Bein (Owner: „anzahl über
    #     schwelle ist besser als 1 absolut"), bei gleichem Höchstalter.
    viele = server.wesen_status(board_md(frisch(3) + "\n" + uralt(4)), today, noarch)
    check("wesen: mehrere überalterte Items eskalieren sehr wohl",
          viele["alter"] > einer["alter"] + 0.3 and viele["strain"] > einer["strain"] + 0.2)
    # (c) Jedes einzelne Häkchen bewegt den Score sichtbar (Bens „spürbar bei jedem Abhaken").
    stufen = [server.wesen_status(board_md(frisch(k)), today, noarch)["strain"]
              for k in (12, 11, 10)]
    check("wesen: jedes Häkchen senkt die Last messbar und monoton",
          stufen[0] > stufen[1] > stufen[2]
          and all(0.02 < a - b < 0.06 for a, b in zip(stufen, stufen[1:])))
    # (d) Führendes Bein — reine Darstellungsinfo (Alter → Augen, Menge → Mund im Frontend).
    check("wesen: das führende Bein wird benannt",
          server.wesen_status(board_md(frisch(14)), today, noarch)["lead"] == "menge"
          and server.wesen_status(board_md(uralt(4)), today, noarch)["lead"] == "alter")

    # (e) Weiche Sättigung (Runde 2, Q1=B): oben gibt es KEIN Plateau mehr. Genau das
    #     war sein Einwand — „22 items = immer schlimmste stufe, egal ob ich gerade von 30
    #     auf 22 runtergekommen bin". Unter der Schwelle bleibt es exakt linear.
    check("wesen: unterhalb der Schwelle bleibt das Bein linear",
          abs(server._soft_cap(0.5) - 0.5) < 1e-9 and abs(server._soft_cap(1.0) - 1.0) < 1e-9)
    hoch = [server._soft_cap(k / server.WESEN_JETZT) for k in (22, 24, 26, 30, 40)]
    check("wesen: oben wächst es weiter, aber gedämpft und unter dem Asymptoten",
          all(a < b for a, b in zip(hoch, hoch[1:]))
          and all(b - a < 0.1 for a, b in zip(hoch, hoch[1:]))
          and hoch[-1] < server.WESEN_LEG_CAP)

    # --- Richtungs-Bein (Runde 2, Q2=C/Q3=B/Q4=C). Ohne Journaldatei DARF es keinen
    # Effekt geben (der Alarm hängt nicht an einer gitignored Datei — auf einem frischen
    # Checkout zählt nur „heute").
    md = board_md(frisch(10))
    old_hist = server.WESEN_HISTORY
    try:
        server.WESEN_HISTORY = Path("/nonexistent/wesen-history.jsonl")
        blank = server.wesen_status(md, today, noarch)["strain"]
        with tempfile.TemporaryDirectory() as td:
            server.WESEN_HISTORY = Path(td) / "hist.jsonl"
            def schreib(jetzt: list[int]) -> None:
                # `oldest_days` bewusst wie auf dem Testboard (Items vom 25.07.) — sonst
                # bewegt sich nicht die MENGE, sondern versehentlich das Alters-Bein.
                server.WESEN_HISTORY.write_text("\n".join(
                    json.dumps({"date": (today - timedelta(days=len(jetzt) - i)).isoformat(),
                                "state": "x", "jetzt": j, "oldest_days": 13,
                                "jetzt_over_14d": 0})
                    for i, j in enumerate(jetzt)), encoding="utf-8")
            # Vorwoche war deutlich voller ⇒ heute ist RUNTERKOMMEN ⇒ Entlastung.
            schreib([30, 28, 26, 24, 22])
            runter = server.wesen_status(md, today, noarch)
            # Vorwoche war ruhig ⇒ heute ist ein Anstieg ⇒ Aufschlag.
            schreib([2, 2, 3, 2, 3])
            rauf = server.wesen_status(md, today, noarch)["strain"]
            check("wesen: Runterkommen entlastet, Hochgehen belastet",
                  runter["strain"] < blank < rauf)
            check("wesen: Entlastung bis zwei Bänder, Aufschlag nur eines (asymmetrisch)",
                  blank - runter["strain"] <= server.WESEN_DIR_RELIEF + 1e-9
                  and rauf - blank <= server.WESEN_DIR_PENALTY + 1e-9
                  and blank - runter["strain"] > server.WESEN_DIR_PENALTY)
            check("wesen: die Richtung steht sichtbar in der Warum-Zeile",
                  "DOWN" in runter["why"])
            # Plateau: heute wie die ganze Woche ⇒ kein Bonus. Der Owner soll für Stillstand
            # nicht belohnt werden, nur für Bewegung.
            schreib([10, 10, 10, 10, 10])
            check("wesen: Plateau auf gleichem Niveau gibt keine Entlastung",
                  abs(server.wesen_status(md, today, noarch)["strain"] - blank) < 0.02)
            # Ein einzelner Vergleichstag ist zu wenig — „Richtung" wäre geraten.
            schreib([30])
            check("wesen: unter zwei Vergleichstagen bleibt die Richtung stumm",
                  server.wesen_status(md, today, noarch)["strain"] == blank)
            # Zeilen ohne "jetzt_over_14d" (alle vor dem 07.08.) werden rekonstruiert.
            server.WESEN_HISTORY.write_text("\n".join(json.dumps(
                {"date": (today - timedelta(days=d)).isoformat(), "state": "burst",
                 "jetzt": 23, "oldest_days": 23, "jetzt_over_7d": 7}) for d in (2, 1)),
                encoding="utf-8")
            check("wesen: Historienzeilen im Altformat werden rekonstruiert",
                  server.wesen_status(md, today, noarch)["strain"] < blank)
            # Eine kaputte Zeile darf die Kachel nicht mitreißen.
            server.WESEN_HISTORY.write_text("{kaputt\n", encoding="utf-8")
            check("wesen: kaputte Historienzeile → Richtung stumm, Zustand steht",
                  server.wesen_status(md, today, noarch)["strain"] == blank)
            # Der Snapshot muss die ROHLAST schreiben, nicht den korrigierten Score —
            # sonst frisst die Richtung morgen ihren eigenen Effekt von heute.
            schreib([30, 28, 26, 24, 22])
            w = server.wesen_status(md, today, noarch)
            check("wesen: Snapshot schreibt die Rohlast, nicht den korrigierten Score",
                  w["strain_raw"] == blank and w["strain"] != w["strain_raw"])
    finally:
        server.WESEN_HISTORY = old_hist


def test_wesen_velocity_und_zuckerregel() -> None:
    """Wesen V2 (22.07., Faden 09d1203ce11a): Board-/Dev-Erledigungen zählen nicht
    positiv, Zufluss/Abfluss als Saldo, Zuckerregel. Der Kern-Bug vorher: Zufluss wurde
    OHNE Dev gezählt, Abfluss MIT — eine Woche Board-Basteln machte das Wesen gesund."""
    from datetime import date as _date
    today = _date(2026, 7, 21)
    noarch = Path("/nonexistent")
    # Ein Board mit produktivem Zufluss (5 frische) + viel abgehaktem Board-Kram.
    prod = "\n".join(f"- [ ] Neu {i} *(2026-07-20)*" for i in range(5))
    boardwork = "\n".join(f"- [x] Board-Kram {i} *(2026-07-20)*" for i in range(6))
    md = (f"## Thema\n\n### Jetzt\n\n{prod}\n### Bald\n\n### Geparkt\n\n"
          f"## Dev (Board)\n\n### Jetzt\n\n{boardwork}\n### Bald\n\n### Geparkt\n")
    b = server.parse_board(md)
    check("scope: Board-Erledigungen füllen den produktiven Abfluss nicht",
          server._done_since(b, "2026-07-14", noarch, "prod") == 0
          and server._done_since(b, "2026-07-14", noarch, "board") == 6
          and server._done_since(b, "2026-07-14", noarch) == 6)  # "all" bleibt die Gesamtzahl
    w = server.wesen_status(b, today, noarch)
    check("wesen: 5 rein, 0 produktiv raus → stuffed (Saldo), Board-Kram rettet nicht",
          w["state"] == "stuffed" and "OUT 0" in w["why"] and "BOARD 6" in w["why"])
    # Zuckerregel: ruhiges Board (kein Zufluss-Überhang), aber die Woche ging fürs Board drauf.
    md2 = ("## Thema\n\n### Jetzt\n\n- [ ] Eins *(2026-07-01)*\n- [x] Echt *(2026-07-20)*\n"
           "### Bald\n\n### Geparkt\n\n## Dev (Board)\n\n### Jetzt\n\n" + boardwork
           + "\n### Bald\n\n### Geparkt\n")
    w2 = server.wesen_status(server.parse_board(md2), today, noarch)
    check("wesen: 6 Board- vs. 1 produktives Item → fat (ZUCKERWERK)",
          w2["state"] == "fat" and "SUGAR WORK" in w2["why"])
    # Gegenprobe: produktiver Abfluss überholt das Zuckerwerk → keine Zuckerregel mehr.
    md3 = ("## Thema\n\n### Jetzt\n\n- [ ] Eins *(2026-07-01)*\n"
           + "\n".join(f"- [x] Echt {i} *(2026-07-20)*" for i in range(7))
           + "\n### Bald\n\n### Geparkt\n\n## Dev (Board)\n\n### Jetzt\n\n" + boardwork
           + "\n### Bald\n\n### Geparkt\n")
    w3 = server.wesen_status(server.parse_board(md3), today, noarch)
    check("wesen: produktiver Abfluss > Board → keine Zuckerregel", w3["state"] != "fat"
          or "ZUCKERWERK" not in w3["why"])
    # Dev (Work) ist KEIN Zuckerwerk — das ist echte Projektarbeit, also die
    # „produktive Arbeit" aus dem Owner-Turn. Nur Board/Tools zählen nicht positiv.
    md4 = ("## Dev (Work)\n\n### Jetzt\n\n- [x] WORK-1 gefixt *(2026-07-20)*\n"
           "### Bald\n\n### Geparkt\n\n## Dev (Tools)\n\n### Jetzt\n\n"
           "- [x] Skript gebaut *(2026-07-20)*\n### Bald\n\n### Geparkt\n")
    b4 = server.parse_board(md4)
    check("scope: Dev (Work) zählt produktiv, Dev (Tools) ist Zuckerwerk",
          server._done_since(b4, "2026-07-14", noarch, "prod") == 1
          and server._done_since(b4, "2026-07-14", noarch, "board") == 1)
    # Archiv-Herkunft: der sweep schreibt „← Thema / Spalte" bzw. „← Person: Name" —
    # inkl. der Namen von VOR der Dev-Umbenennung (22.07.), die im Archiv weiterleben.
    arch = Path(tempfile.mkdtemp()) / "board-archive.md"
    arch.write_text("# Archiv\n\n## 2026-07-20\n\n"
                    "- [x] Echte Arbeit *(2026-07-20)* ← Produkt & Strategie / Jetzt\n"
                    "- [x] Board-Kram *(2026-07-20)* ← Dev (Board) / Jetzt\n"
                    "- [x] Altes Board-Kram *(2026-07-20)* ← Code & Tools / Jetzt\n"
                    "- [x] Personen-Notiz *(2026-07-20)* ← Person: Eric\n"
                    "- [x] Uralt ohne Herkunft *(2026-07-20)*\n", encoding="utf-8")
    leer = server.parse_board("## Thema\n\n### Jetzt\n\n### Bald\n\n### Geparkt\n")
    check("archiv-scope: prod (1 echte + 1 herkunftslose), board (Dev + Legacy-Name), Person zählt nirgends",
          server._done_since(leer, "2026-07-14", arch, "prod") == 2
          and server._done_since(leer, "2026-07-14", arch, "board") == 2
          and server._done_since(leer, "2026-07-14", arch) == 5)


def test_attention_hints() -> None:
    """Attention-Zeile (E6): deterministische Regeln — Fälliges beim Namen (max 3 + Rest),
    Jetzt-über-Limit pro Thema, Freitags-Ziel + Wochen-Telemetrie NUR freitags.
    Datum überall injiziert; 2026-07-24 ist ein Freitag, 2026-07-20 ein Montag."""
    from datetime import date as _date
    monday, friday = _date(2026, 7, 20), _date(2026, 7, 24)
    sb = server.parse_board(COCKPIT_SYNTH)
    hints = server.attention_hints(sb, monday, archive=Path("/nonexistent"))
    kinds = [h["kind"] for h in hints]
    check("attention: Montag → kein Freitags-Hint", "friday" not in kinds)
    # Fixture-Item „Überfällig" hat !(2026-07-18) → am Mo 20.07. seit 2 Tagen überfällig
    check("attention: überfälliges Item beim Namen mit Tageszahl",
          any(h["kind"] == "due" and "Überfällig — overdue by 2 days" in h["text"] for h in hints))
    check("attention: kein Limit-Hint bei 6 offenen? (Fixture hat 5 offene in Jetzt)",
          "limit" not in kinds)

    # Freitag: Ziel-Zeile + Telemetrie; Archiv-Fixture liefert die Wochensumme
    with tempfile.TemporaryDirectory() as td:
        arch = Path(td) / "board-archive.md"
        arch.write_text("# Board-Archiv\n\n## 2026-07-13\n\n- [x] Vorwoche ← X / Jetzt\n\n"
                        "## 2026-07-21\n\n- [x] A ← X / Jetzt\n- [x] B ← X / Jetzt\n")
        fh = server.attention_hints(sb, friday, archive=arch)
        fr = [h["text"] for h in fh if h["kind"] == "friday"]
        check("attention: Freitag → Ziel-Zeile mit offener Jetzt-Zahl",
              any("Friday goal" in t and "still open" in t for t in fr))
        # Diese Woche (ab Mo 20.07.): Archiv-Abschnitt 21.07. = 2 Items; das Board-done
        # c…6 trägt date 2026-07-10 (Vorwoche) und zählt NICHT → 2
        check("attention: Telemetrie zählt Archiv dieser Woche, Vorwoche nicht",
              any("Completed this week: 2 items" in t for t in fr))

    # Über-Limit: Thema mit 6 offenen Jetzt-Items
    many = "## Voll\n\n### Jetzt\n\n" + "".join(f"- [ ] Item {i}\n\n" for i in range(6)) + \
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n"
    lh = server.attention_hints(server.parse_board(many), monday, archive=Path("/nonexistent"))
    check("attention: Jetzt über Limit (6/5) gemeldet",
          any(h["kind"] == "limit" and "Voll" in h["text"] and "6/5" in h["text"] for h in lh))
    # Kappung liegt seit 2026-08-07 im Frontend (renderAttention, ATT_CAP): der Server
    # liefert ALLE fälligen Items mit Thema + Gruppe, damit der Weekend-Filter greifen
    # kann, BEVOR „die ersten 3" bestimmt werden. Vorher kappte der Server auf 3 — dann
    # hätten im Weekend-Modus drei weggefilterte Work-Zeilen die privaten verdeckt.
    lots = "## T\n\n### Jetzt\n\n" + "".join(f"- [ ] D{i} !(2026-07-0{i + 1})\n\n" for i in range(5)) + \
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n"
    dh = [h for h in server.attention_hints(server.parse_board(lots), monday, archive=Path("/nonexistent"))
          if h["kind"] == "due"]
    check("attention: Fälliges kommt ungekappt (Kappung macht das Frontend)", len(dh) == 5)
    check("attention: jeder Fälligkeits-Hinweis trägt Thema + Gruppe für den Weekend-Filter",
          all(h.get("group") == "due" and h.get("theme") == "T" for h in dh))


def test_context_tokens_extraction() -> None:
    """usage → Kontextgröße: iterations[-1] zählt (Top-Level summiert über alle
    Iterationen und würde bei Multi-Turn-Runs überzählen); ohne usage → 0/kein Stempel."""
    env = {"result": "ok", "session_id": "fa4e5e55-0000-4000-8000-00000000e2e1",
           "permission_denials": [], "subtype": "success", "is_error": False,
           "usage": {"input_tokens": 99, "cache_read_input_tokens": 99, "cache_creation_input_tokens": 99,
                     "iterations": [
                         {"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3},
                         {"input_tokens": 10, "cache_read_input_tokens": 60000, "cache_creation_input_tokens": 5000}]}}
    out = gc_runner._parse_claude_stdout(json.dumps(env), "", 0)
    check("ctx: letzte Iteration gezählt", out["context_tokens"] == 65010)
    with tempfile.TemporaryDirectory() as td:
        _text, _session, gc_last = gc_runner._outcome(out, "id1", "T", Path(td))
        check("ctx: gc_last = ~65k + Zeitstempel", gc_last.startswith("~65k · 20"))
        env.pop("usage")
        out2 = gc_runner._parse_claude_stdout(json.dumps(env), "", 0)
        # Seit 2026-08-25 stempelt ein Erfolg AUCH ohne lesbare Zahlen: vorher fiel mit dem
        # Kontext auch die Kostenangabe weg und am Item blieb still der Stempel des Vorlaufs
        # stehen — der Run sah aus wie nie gelaufen.
        check("ctx: ohne usage → 0, aber Stempel bleibt", out2["context_tokens"] == 0
              and gc_runner._outcome(out2, "id1", "T", Path(td))[2].startswith("~0k · 20"))
        out2["ok"] = False
        out2["context_tokens"] = 12345
        # Verhalten am 2026-07-23 bewusst gedreht (Blatt Q3=A): vorher stempelte ein
        # Fehllauf NICHTS — ein toter Run war am Item unsichtbar. Jetzt ❌ statt Kontextgröße;
        # die Kontextzahl eines Fehllaufs wäre ohnehin nicht die eines resumebaren Stands.
        fail_stamp = gc_runner._outcome(out2, "id1", "T", Path(td))[2]
        check("ctx: Fehllauf stempelt ❌ statt Kontextgröße",
              fail_stamp.startswith("❌ · ") and "12k" not in fail_stamp)


COMPACT_SYNTH = """## Thema

### Jetzt

- [ ] Mit Session *(2026-07-10)*
  @gc-id: cccccccccccc
  @gc: frage
  @gc-re: antwort
  @gc-session: fa4e5e55-0000-4000-8000-0000000000cc · board-mit-session

- [ ] Ohne Session *(2026-07-10)*
  @gc-id: dddddddddddd
  @gc: frage

- [ ] Codex Session *(2026-07-10)*
  @gc-id: eeeeeeeeeeee
  @gc: frage
  @gc-session: 019ff158-1a77-7e23-a685-366b4e0f391b · board-codex · codex

# Personen

# Notizen
"""


def test_gc_compact_endpoint() -> None:
    """POST /api/gc-compact (2026-07-16 Q4=A): /compact auf die bestehende Session.
    Erfolg stempelt @gc-last ("kompaktiert · …") OHNE Faden-Turn; ohne Session/ohne id
    → 4xx; Fehler → ❌-Reply im Faden (fail gracefully)."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(COMPACT_SYNTH)
    with tempfile.TemporaryDirectory() as td:
        server.CLAUDE_BIN = _fake_claude(Path(td), "ok", OK_JSON)
        httpd, port = _serve(Path(tmp))
        try:
            code, _ = _post(port, "/api/gc-compact", {})
            check("compact: ohne id → 400", code == 400)
            code2, r2 = _post(port, "/api/gc-compact", {"id": "dddddddddddd"})
            check("compact: ohne Session → 409", code2 == 409 and "session" in r2.get("error", ""))
            codec, rc = _post(port, "/api/gc-compact", {"id": "eeeeeeeeeeee"})
            check("compact: Codex wird nicht als Claude-Session behandelt",
                  codec == 409 and "Codex" in rc.get("error", ""))
            code3, r3 = _post(port, "/api/gc-compact", {"id": "cccccccccccc"})
            check("compact: 202 accepted", code3 == 202 and r3.get("ok"))
            deadline = time.time() + 10
            while time.time() < deadline and "kompaktiert" not in Path(tmp).read_text():
                time.sleep(0.1)
            text = Path(tmp).read_text()
            check("compact: @gc-last gestempelt", "@gc-last: kompaktiert · 20" in text)
            board = server.parse_board(text)
            it = next(x for _s, _n, _c, x in server._all_items(board) if x["id"] == "cccccccccccc")
            check("compact: kein zusätzlicher Faden-Turn", [e["kind"] for e in it["thread"]] == ["ask", "reply"])
            check("compact: Session unangetastet", it["session"].startswith("fa4e5e55-0000-4000-8000-0000000000cc"))
            deadline = time.time() + 5
            while time.time() < deadline and json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/board")).get("running"):
                time.sleep(0.1)
            rb = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board"))
            check("compact: Registry wieder leer", rb.get("running") == [] and rb.get("compacting") == [])

            # Fehlerpfad: kaputtes Binary → sichtbares ❌ im Faden statt stillem Verschlucken
            server.CLAUDE_BIN = _fake_claude(Path(td), "boom", 'print("kein json"); sys.exit(2)')
            code4, _ = _post(port, "/api/gc-compact", {"id": "cccccccccccc"})
            check("compact: Fehllauf trotzdem 202 (async)", code4 == 202)
            deadline = time.time() + 10
            while time.time() < deadline and "❌ Compaction failed" not in Path(tmp).read_text():
                time.sleep(0.1)
            check("compact: Fehler sichtbar im Faden", "❌ Compaction failed" in Path(tmp).read_text())
        finally:
            httpd.shutdown()
            server.CLAUDE_BIN = server.DEFAULT_CLAUDE_BIN
            Path(tmp).unlink(missing_ok=True)


def test_turn_times_are_derived_not_stored() -> None:
    """Faden-Turns tragen eine Uhrzeit, ohne dass board.md ein Feld dazubekommt
    (2026-08-13: „dann könnte ich die Historie besser einsehen").

    Quelle ist der Sidecar-Dateiname. Der Test hält beide Hälften fest: die Zeit MUSS
    ankommen, und die Serialisierung darf sich davon nicht anfassen lassen — sonst
    wandert ein Anzeigefeld in die Datei und die Round-Trip-Invariante fällt.
    """
    import sidecar
    check("turn_time: Zeit aus dem Sidecar-Namen",
          sidecar.turn_time("Kurzsatz … → volle Antwort: inbox/gc-threads/ab12cd34-20260813-133108-d6c6.md")
          == "2026-08-13 13:31")
    check("turn_time: kurzer Turn ohne Datei hat keine Zeit",
          sidecar.turn_time("push") is None)
    check("turn_time: Verweis ohne Zeitstempel im Namen → None (statt zu raten)",
          sidecar.turn_time("x … → voller Text: inbox/gc-threads/handgemalt.md") is None)
    check("turn_time: Verweis mitten im Satz zählt nicht (wie expand)",
          sidecar.turn_time("zitat → volle Antwort: inbox/gc-threads/ab-20260813-133108-d6c6.md und mehr") is None)

    text = ("# Board\n\n## Thema\n\n### Jetzt\n\n"
            "- [ ] Item *(2026-08-13)*\n"
            "  @gc-id: ab12cd34ef56\n"
            "  @gc: Frage … → voller Text: inbox/gc-threads/ab12cd34ef56-20260813-090000-aa11.md\n"
            "  @gc-re: kurze Antwort ohne Sidecar\n")
    board = server.parse_board(text)
    it = board["themes"][0]["cols"]["Jetzt"][0]
    vorher = server.item_lines(it)
    server.annotate_turn_times(board)
    check("annotate: ausgelagerter Turn bekommt at", it["thread"][0].get("at") == "2026-08-13 09:00")
    check("annotate: kurzer Turn bleibt ohne at", "at" not in it["thread"][1])
    check("annotate: Serialisierung unverändert (at ist Anzeige, kein Inhalt)",
          server.item_lines(it) == vorher)

    # Chronologie-Wächter: die Diät-Migration (2026-07-17, 24 Dateien in einer Minute) hat
    # Altbestand nachträglich ausgelagert — solche Stempel liegen NACH den Antworten, die
    # sie ausgelöst haben. Ungefiltert stand im Board „Frage 2 Tage nach der Antwort".
    migriert = ["2026-07-17 00:39", "2026-07-15 09:40", "2026-07-17 00:39", "2026-07-15 10:31"]
    check("chrono: Migrationsstempel fallen raus, die echte Kette bleibt",
          server._believable(migriert) == [None, "2026-07-15 09:40", None, "2026-07-15 10:31"])
    check("chrono: saubere Kette bleibt vollständig",
          server._believable(["2026-08-01 10:00", None, "2026-08-01 10:05"])
          == ["2026-08-01 10:00", None, "2026-08-01 10:05"])
    check("chrono: ein einzelner Ausreißer verwirft nicht den ganzen Faden",
          server._believable(["2026-09-01 08:00", "2026-08-01 10:00", "2026-08-01 10:05", "2026-08-01 10:09"])
          == [None, "2026-08-01 10:00", "2026-08-01 10:05", "2026-08-01 10:09"])


def test_board_diet_sidecar_module() -> None:
    """board.md-Diät (beschlossen 2026-07-16): geteiltes sidecar-Modul. Kurze Turns
    unverändert, lange komplett in Datei + Kurzsatz/Verweis inline, Expansion streng
    typisiert (basename-only, nur am Zeilenende, fehlende Datei → None)."""
    import config as _cfg
    import sidecar
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        check("diet: kurzer Turn bleibt unverändert",
              sidecar.inline_turn("id1", "T", "kurz und gut", sd) == "kurz und gut")
        long = "Erster Satz der Frage. " + "x" * 600
        line = sidecar.inline_turn("id1", "Mein Item", long, sd, kind="ask")
        files = list(sd.glob("id1-*.md"))
        check("diet: langer ask → genau 1 Sidecar-Datei", len(files) == 1)
        check("diet: Volltext liegt im Sidecar", long in files[0].read_text())
        check("diet: Sidecar-Header ist Owner-Turn",
              f"{_cfg.OWNER} turn: Mein Item" in files[0].read_text())
        check("diet: inline = Kurzsatz + voller-Text-Verweis",
              line.startswith("Erster Satz der Frage.") and "→ full text: " in line and len(line) < 400)
        check("diet: Verweis-Zeile wird nie doppelt ausgelagert",
              sidecar.inline_turn("id1", "T", "kurz … → voller Text: inbox/gc-threads/a.md", sd)
              == "kurz … → voller Text: inbox/gc-threads/a.md")
        # Expansion: Datei in sd, Referenz per Basename
        (sd / "ok.md").write_text("VOLLTEXT-INHALT")
        check("diet: expand liest referenzierte Datei",
              sidecar.expand("kurz … → volle Antwort: inbox/gc-threads/ok.md", sd) == "VOLLTEXT-INHALT")
        check("diet: expand — fehlende Datei → None (fail gracefully)",
              sidecar.expand("kurz … → voller Text: inbox/gc-threads/fehlt.md", sd) is None)
        check("diet: expand — Verweis mitten im Satz zählt NICHT (Injection-Härtung)",
              sidecar.expand("zitat → volle Antwort: inbox/gc-threads/ok.md und mehr Text", sd) is None)
        check("diet: expand — Traversal-Namen matchen nicht",
              sidecar.expand("x … → voller Text: inbox/gc-threads/../../secret.md", sd) is None)


def test_board_diet_append_and_prompt() -> None:
    """Symmetrie-Kern der Diät: /api/gc-append lagert lange @gc:-Turns aus (wie bisher
    nur Antworten), und build_prompt expandiert NUR den neuesten Auftrag (Lazy Loading,
    Runde 2) — ältere Verweise bleiben Kurzzeile, fehlende Sidecars sichtbar."""
    import sidecar
    with tempfile.TemporaryDirectory() as td:
        board = Path(td) / "board.md"
        board.write_text(SYNTH)
        httpd, port = _serve(board)
        try:
            long_ask = "Kernauftrag: bitte X bauen. " + "y" * 600
            code, r = _post(port, "/api/gc-append",
                            {"kind": "ask", "text": long_ask, "addr": {"id": "bbbbbbbbbbbb"}})
            text = board.read_text()
            side = list((Path(td) / "gc-threads").glob("bbbbbbbbbbbb-*.md"))
            check("diet-append: langer @gc: → 200 + Sidecar-Datei", code == 200 and len(side) == 1)
            check("diet-append: Volltext im Sidecar", long_ask in side[0].read_text())
            check("diet-append: inline Kurzsatz + Verweis, keine Riesenzeile",
                  "Kernauftrag: bitte X bauen." in text and "→ full text: " in text
                  and long_ask not in text)
            check("diet-append: kurzer Turn weiterhin wortgleich inline",
                  _post(port, "/api/gc-append", {"kind": "ask", "text": "kurz",
                                                 "addr": {"id": "aaaaaaaaaaaa"}})[0] == 200
                  and "@gc: kurz" in board.read_text())
        finally:
            httpd.shutdown()

    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        (sd / "alt.md").write_text("ALTER-VOLLTEXT")
        (sd / "neu.md").write_text("NEUER-VOLLTEXT")
        pending = {"addr": {"id": "x", "scope": "theme", "name": "T", "col": "Jetzt"},
                   "title": "Item", "body": [], "session": "",
                   "thread": [
                       {"kind": "ask", "text": "alte frage … → voller Text: inbox/gc-threads/alt.md"},
                       {"kind": "reply", "text": "antwort … → volle Antwort: inbox/gc-threads/alt.md"},
                       {"kind": "ask", "text": "neue frage … → voller Text: inbox/gc-threads/neu.md"}],
                   "last_ask": "neue frage … → voller Text: inbox/gc-threads/neu.md"}
        fresh = gc_runner.build_prompt(pending, resume=False, sidecar_dir=sd)
        check("diet-prompt: neuester Auftrag voll expandiert", "NEUER-VOLLTEXT" in fresh)
        check("diet-prompt: alte Turns bleiben Kurzzeile (lazy)", "ALTER-VOLLTEXT" not in fresh)
        check("diet-prompt: Kontrakt erklärt on-demand-Lesen", "inbox/gc-threads/" in gc_runner.PROMPT_CONTRACT)
        res = gc_runner.build_prompt(pending, resume=True, sidecar_dir=sd)
        check("diet-prompt: Resume expandiert den neuen Turn ebenfalls", "NEUER-VOLLTEXT" in res)
        pending["last_ask"] = "weg … → voller Text: inbox/gc-threads/nicht-da.md"
        gone = gc_runner.build_prompt(pending, resume=True, sidecar_dir=sd)
        check("diet-prompt: fehlender Sidecar → sichtbarer Marker statt Crash",
              "Sidecar is missing" in gone)
        # Kontrakt-Diät (Proposal B, Owner-Go 2026-07-20): Resume = Kurz-Reminder statt
        # Voll-Kontrakt; nach Board-Compact (@gc-last "kompaktiert…") einmalig wieder voll.
        marker_full = "Authentication boundary"  # appears only in the full contract
        check("kontrakt-diät: fresh trägt Voll-Kontrakt", marker_full in fresh)
        check("kontrakt-diät: Resume trägt Kurz-Reminder statt Voll-Kontrakt",
              "short reminder" in res and marker_full not in res)
        pending["gc_last"] = "kompaktiert · 2026-07-20 12:00"
        comp = gc_runner.build_prompt(pending, resume=True, sidecar_dir=sd)
        check("kontrakt-diät: nach Compact einmalig wieder Voll-Kontrakt", marker_full in comp)
        pending["gc_last"] = "~85k · 2026-07-20 13:00"  # nächster Run überstempelt
        again = gc_runner.build_prompt(pending, resume=True, sidecar_dir=sd)
        check("kontrakt-diät: nach ueberstempeltem @gc-last wieder Reminder", marker_full not in again)
        # Leak 4 (Token-Optimierungs-Faden, 2026-07-21): frische Runs auf langen Fäden
        # tragen nur die letzten THREAD_TAIL_TURNS Turns als Text + Auslassungszeile.
        pending["thread"] = ([{"kind": "reply", "text": f"turn-{i}"} for i in range(45)]
                             + [{"kind": "ask", "text": "neue frage … → voller Text: inbox/gc-threads/neu.md"}])
        pending["last_ask"] = "neue frage … → voller Text: inbox/gc-threads/neu.md"
        capped = gc_runner.build_prompt(pending, resume=False, sidecar_dir=sd)
        check("thread-cap: älteste Turns fliegen raus, jüngste bleiben",
              "turn-0" not in capped and "turn-44" in capped)
        check("thread-cap: Auslassungszeile nennt Anzahl + Fundort",
              "16 older turns omitted" in capped and "inbox/board.md" in capped)
        check("thread-cap: neuester Auftrag weiter voll expandiert", "NEUER-VOLLTEXT" in capped)


def test_board_diet_migration() -> None:
    """Einmalige Migration (migrate_diet.py): lagert Alt-Riesenzeilen aus, verifiziert
    Sidecar-Inhalt vor dem Schrumpfen, Roundtrip bleibt verlustfrei, idempotent."""
    import sys

    import migrate_diet
    import sidecar
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "inbox" / "gc-threads").mkdir(parents=True)
        board = root / "inbox" / "board.md"
        long_ask = "Riesiger Alt-Turn. " + "z" * 800
        board.write_text(SYNTH.replace("@gc: zweite frage, gleicher titel+datum",
                                       f"@gc: {long_ask}"))
        before = board.read_text()
        old_root, old_sd = sidecar.GC_ROOT, sidecar.SIDECAR_DIR
        old_argv = sys.argv
        sidecar.GC_ROOT, sidecar.SIDECAR_DIR = root, root / "inbox" / "gc-threads"
        try:
            sys.argv = ["migrate_diet.py", "--file", str(board)]
            rc = migrate_diet.main()
            after = board.read_text()
            side = list((root / "inbox" / "gc-threads").glob("*.md"))
            check("diet-migration: exit 0 + Datei geschrumpft", rc == 0 and len(after) < len(before))
            check("diet-migration: genau 1 Turn ausgelagert, Volltext im Sidecar",
                  len(side) == 1 and long_ask in side[0].read_text())
            check("diet-migration: Verweis inline, Riesenzeile weg",
                  "→ full text: inbox/gc-threads/" in after and long_ask not in after)
            b = server.parse_board(after)
            check("diet-migration: Roundtrip verlustfrei (lost=0, Turn-Zahl gleich)",
                  server.lost_total(after, b) == 0
                  and sum(len(it["thread"]) for _s, _n, _c, it in server._all_items(b))
                  == sum(len(it["thread"]) for _s, _n, _c, it in server._all_items(server.parse_board(before))))
            rc2 = migrate_diet.main()
            check("diet-migration: idempotent (2. Lauf ändert nichts)",
                  rc2 == 0 and board.read_text() == after)
        finally:
            sidecar.GC_ROOT, sidecar.SIDECAR_DIR = old_root, old_sd
            sys.argv = old_argv


RITUALE_FIXTURE = {
    "active_from": "2026-01-01",
    "rituale": {
        "reflection": {"kind": "daily", "title": "Reflection ✍️", "deadline": "11:00",
                       "proof": "multiline", "prompt": "Wo warst du gestern outside the comfort zone?",
                       "persist_personal": ""},  # pro Test überschrieben (Temp-Pfad)
        "social": {"kind": "daily", "title": "Social-Kontakt", "deadline": "14:00",
                   "proof": "single", "prompt": "An wen geschrieben / worüber?"},
        "weekend": {"kind": "weekly", "title": "Wochenendplanung",
                    "appears": {"weekday": 1, "time": "17:00"},
                    "deadline": {"weekday": 2, "time": "17:00"},
                    "proof": "multiline", "prompt": "Plan in Stichworten"},
        # Generisches daily-Ritual mit spätem "appears" + proof:"none" (One-Click, kein Modal).
        # Testet die generischen Fähigkeiten, die früher das Fake-Ritual "feierabend" nutzte —
        # das ist seit 24.07. kein Ritual mehr (reine Wanduhr-Pill im Frontend), die Config-
        # Fähigkeiten bleiben aber und werden hier weiter abgedeckt.
        "nightcap": {"kind": "daily", "title": "Nightcap 🌙",
                     "appears": "21:30", "deadline": "23:00", "proof": "none"},
    },
}


def _ritual_env(tmp_path: Path, persist: Path | None = None):
    """rituale.json + Journal-Pfad in einen Temp-Ordner umbiegen — nie gegen die echten
    Dateien testen (Journal UND persist_personal, die Therapie-Ablage ist tabu)."""
    cfg = json.loads(json.dumps(RITUALE_FIXTURE))
    if persist is not None:
        cfg["rituale"]["reflection"]["persist_personal"] = str(persist)
    rfile = tmp_path / "rituale.json"
    rfile.write_text(json.dumps(cfg))
    return rfile, tmp_path / "journal" / "rituale.jsonl"


def test_ritual_status_daily_weekly() -> None:
    """Status-Logik daily/weekly mit gefakter Uhrzeit: hidden/open/overdue/done + das
    Weekly-Fenster über die Tagesgrenze Di→Mi (Mock-Review: „längere Deadline")."""
    from datetime import date as _date
    from datetime import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        rfile, jfile = _ritual_env(Path(td))
        old_rfile, old_jfile, old_now = server.RITUALE_FILE, server.RITUAL_JOURNAL, server.ritual_now
        server.RITUALE_FILE, server.RITUAL_JOURNAL = rfile, jfile
        try:
            cfg = (server.load_rituale()["rituale"])
            refl = cfg["reflection"]

            # --- daily: hidden vor 06:00, open danach, overdue nach 11:00, done nach Proof
            st = server.ritual_instance("reflection", refl, _dt(2026, 7, 22, 3, 0), [], "2026-01-01")
            check("ritual: daily 03:00 → hidden", st["status"] == "hidden")
            st = server.ritual_instance("reflection", refl, _dt(2026, 7, 22, 8, 0), [], "2026-01-01")
            check("ritual: daily 08:00 → open", st["status"] == "open")
            st = server.ritual_instance("reflection", refl, _dt(2026, 7, 22, 11, 30), [], "2026-01-01")
            check("ritual: daily 11:30 (nach Deadline 11:00) → overdue", st["status"] == "overdue")
            done_ev = {"ritual": "reflection", "cycle": "2026-07-22", "kind": "done",
                       "ts": "2026-07-22T09:42:00"}
            st = server.ritual_instance("reflection", refl, _dt(2026, 7, 22, 12, 0), [done_ev], "2026-01-01")
            check("ritual: done-Event im Zyklus → done + done_at", st["status"] == "done"
                  and st["done_at"] == "2026-07-22T09:42:00")
            # active_from in der Zukunft ⇒ hidden, obwohl die Uhrzeit sonst "open" wäre
            st = server.ritual_instance("reflection", refl, _dt(2026, 7, 22, 8, 0), [], "2026-07-23")
            check("ritual: vor active_from → hidden trotz Uhrzeit", st["status"] == "hidden")

            # --- weekly: Fenster Di 17:00 → Mi 17:00, spannt über Mitternacht
            weekend = cfg["weekend"]
            st = server.ritual_instance("weekend", weekend, _dt(2026, 7, 21, 16, 0), [], "2026-01-01")  # Di 16:00
            check("ritual: weekly vor Di 17:00 → hidden", st["status"] == "hidden")
            st = server.ritual_instance("weekend", weekend, _dt(2026, 7, 21, 18, 0), [], "2026-01-01")  # Di 18:00
            check("ritual: weekly Di 18:00 (im Fenster) → open", st["status"] == "open")
            st = server.ritual_instance("weekend", weekend, _dt(2026, 7, 22, 0, 30), [], "2026-01-01")  # Mi 00:30
            check("ritual: weekly Mi 00:30 (über Mitternacht, noch im Fenster) → open", st["status"] == "open")
            st = server.ritual_instance("weekend", weekend, _dt(2026, 7, 22, 18, 0), [], "2026-01-01")  # Mi 18:00
            check("ritual: weekly nach Mi 17:00 (Fenster vorbei) → hidden", st["status"] == "hidden")
            done_w = {"ritual": "weekend", "cycle": "2026-07-21", "kind": "done", "ts": "2026-07-21T19:00:00"}
            st = server.ritual_instance("weekend", weekend, _dt(2026, 7, 21, 20, 0), [done_w], "2026-01-01")
            check("ritual: weekly done-Event im aktuellen Fenster → done", st["status"] == "done")

            # --- rituale_status(): alle 4 in einem Aufruf, injizierte Uhr
            server.ritual_now = lambda: _dt(2026, 7, 22, 8, 0)
            all_st = {r["id"]: r for r in server.rituale_status()}
            check("ritual: rituale_status liefert alle 4",
                  set(all_st) == {"reflection", "social", "weekend", "nightcap"})

            # --- daily mit "appears" (nightcap, 21:30 statt Default 06:00) — die anderen
            # dailies (reflection/social) bleiben unverändert bei 06:00, s.o. Deckt die
            # generische appears-Fähigkeit ab (früher vom Fake-Ritual "feierabend" genutzt).
            nc = cfg["nightcap"]
            st = server.ritual_instance("nightcap", nc, _dt(2026, 7, 22, 21, 29), [], "2026-01-01")
            check("ritual: nightcap 21:29 (vor appears) → hidden", st["status"] == "hidden")
            st = server.ritual_instance("nightcap", nc, _dt(2026, 7, 22, 21, 30), [], "2026-01-01")
            check("ritual: nightcap 21:30 (appears) → open", st["status"] == "open")
            st = server.ritual_instance("nightcap", nc, _dt(2026, 7, 22, 22, 59), [], "2026-01-01")
            check("ritual: nightcap 22:59 → noch open", st["status"] == "open")
            st = server.ritual_instance("nightcap", nc, _dt(2026, 7, 22, 23, 0), [], "2026-01-01")
            check("ritual: nightcap 23:00 (Deadline) → overdue", st["status"] == "overdue")
            st = server.ritual_instance("nightcap", nc, _dt(2026, 7, 23, 0, 5), [], "2026-01-01")
            check("ritual: nightcap Folgetag 00:05 → wieder hidden (Zyklus endet an Mitternacht)",
                  st["status"] == "hidden")
        finally:
            server.RITUALE_FILE, server.RITUAL_JOURNAL, server.ritual_now = old_rfile, old_jfile, old_now


def test_ritual_done_endpoint() -> None:
    """POST /api/ritual-done: leerer Proof → 400; sonst Journal-Append + persist_personal
    NUR angehängt (nie überschrieben) — Test schreibt in einen TEMP-Pfad, NIE in die
    echte personal/-Datei."""
    from datetime import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        persist = Path(td) / "reflections.md"
        persist.write_text("# Bestehender Inhalt\n\n## 2026-07-01\nAlter Eintrag.\n\n")
        rfile, jfile = _ritual_env(Path(td), persist=persist)
        old_rfile, old_jfile = server.RITUALE_FILE, server.RITUAL_JOURNAL
        old_now = server.ritual_now
        server.RITUALE_FILE, server.RITUAL_JOURNAL = rfile, jfile
        server.ritual_now = lambda: _dt(2026, 7, 22, 9, 0)
        fd, tmp = tempfile.mkstemp(suffix=".md")
        Path(tmp).write_text(SYNTH)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/ritual-done", {"id": "reflection", "proof": ""})
            check("ritual-done: leerer Proof → 400", code == 400)
            code, r = _post(port, "/api/ritual-done", {"id": "unbekannt", "proof": "x"})
            check("ritual-done: unbekanntes Ritual → 404", code == 404)
            code, r = _post(port, "/api/ritual-done", {"id": "reflection", "proof": "Heute war ich mutig."})
            check("ritual-done: 200 mit Proof", code == 200 and r.get("ok") is True)
            journal = jfile.read_text().strip().splitlines()
            check("ritual-done: Journal-Append (1 done-Event)", len(journal) == 1
                  and json.loads(journal[0])["kind"] == "done"
                  and json.loads(journal[0])["proof"] == "Heute war ich mutig.")
            persisted = persist.read_text()
            check("ritual-done: persist_personal appended, NICHT überschrieben",
                  "Alter Eintrag." in persisted and "## 2026-07-22\nHeute war ich mutig.\n\n" in persisted)
            # Zweiter Aufruf im selben Zyklus (gleicher Tag) darf den Status auf "done" heben,
            # ohne den vorhandenen Journal-Eintrag zu verlieren — status-Query zeigt done.
            st = next(r for r in server.rituale_status(server.ritual_now())
                      if r["id"] == "reflection")
            check("ritual-done: rituale_status zeigt done danach", st["status"] == "done")
        finally:
            httpd.shutdown()
            Path(tmp).unlink(missing_ok=True)
            server.RITUALE_FILE, server.RITUAL_JOURNAL = old_rfile, old_jfile
            server.ritual_now = old_now


def test_ritual_done_proof_none_and_idempotent() -> None:
    """`proof: "none"` (nightcap): leerer Client-Proof wird akzeptiert, Server kanonisiert
    den gespeicherten Text selbst (Client-Wert wird ignoriert, nicht nur toleriert) — und ein
    zweiter Aufruf im selben Zyklus (Doppelklick am One-Click-Pfad ohne Modal-Submit-Lock)
    häng KEIN zweites Journal-Event an (Server-seitige Idempotenz, unabhängig vom
    Client-seitigen In-flight-Guard in index.html)."""
    from datetime import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        rfile, jfile = _ritual_env(Path(td))
        old_rfile, old_jfile, old_now = server.RITUALE_FILE, server.RITUAL_JOURNAL, server.ritual_now
        server.RITUALE_FILE, server.RITUAL_JOURNAL = rfile, jfile
        server.ritual_now = lambda: _dt(2026, 7, 22, 22, 0)  # nightcap ist "open" (21:30–23:00)
        fd, tmp = tempfile.mkstemp(suffix=".md")
        Path(tmp).write_text(SYNTH)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/ritual-done", {"id": "nightcap", "proof": ""})
            check("ritual-done proof:none: leerer Client-Proof → 200 (kein 400 wie bei den anderen)",
                  code == 200 and r.get("ok") is True)
            journal = jfile.read_text().strip().splitlines()
            check("ritual-done proof:none: genau 1 Journal-Event", len(journal) == 1)
            stored = json.loads(journal[0])
            check("ritual-done proof:none: Server kanonisiert den Proof-Text (nicht der leere Client-Wert)",
                  stored["proof"] == "(no proof required)")
            # Client könnte theoretisch Text mitschicken (sollte er laut index.html nicht) —
            # auch der wird ignoriert, der kanonische Server-Text gewinnt.
            code, r = _post(port, "/api/ritual-done", {"id": "nightcap", "proof": "eingeschleuster Text"})
            check("ritual-done proof:none: zweiter Aufruf (auch mit Text) → 200, aber idempotent",
                  code == 200 and r.get("already_done") is True)
            journal2 = jfile.read_text().strip().splitlines()
            check("ritual-done proof:none: immer noch nur 1 Journal-Event (kein Duplikat)",
                  len(journal2) == 1)
        finally:
            httpd.shutdown()
            Path(tmp).unlink(missing_ok=True)
            server.RITUALE_FILE, server.RITUAL_JOURNAL, server.ritual_now = old_rfile, old_jfile, old_now


_RACE_WRITER = """
import os, random, sys, time
from pathlib import Path
sys.path.insert(0, {board_dir!r})
import server
board, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
random.seed(hash(tag))
for i in range(n):
    with server.board_write_guard(board):
        raw = board.read_text()
        time.sleep(random.uniform(0.001, 0.004))   # Fenster zwischen Read und Write
        tmp = board.with_suffix(".tmp." + tag)
        tmp.write_text(raw + tag + "-" + str(i) + "\\n")
        os.replace(tmp, board)
"""


def test_zwei_prozess_race_haelt_den_flock() -> None:
    """Der einzige Test, der die PROZESS-Grenze anfasst (Analyse 06.08., Item 4b1792bf0110).

    Alle anderen Nebenläufigkeits-Tests hier laufen in EINEM Prozess — sie beanspruchen
    damit den `threading.Lock` in `board_write_guard`, aber nie den `fcntl.flock`
    darunter. Genau der ist aber die Hälfte, die am 16.07. als P0 fehlte: `sweep.py`
    schrieb als eigener Prozess ungeschützt am Server vorbei. Ein neuer Schreibpfad, der
    den Guard vergisst, fällt sonst erst im Betrieb auf.

    Zwei echte Subprozesse, je 25 Read-Modify-Write-Zyklen mit künstlichem Fenster.
    Erwartung: kein einziger Schreibvorgang geht verloren. Gemessene Gegenprobe (nicht
    asserted, weil timing-abhängig): dieselben Prozesse OHNE den Guard verlieren rund
    die Hälfte."""
    n = 25
    body = _RACE_WRITER.format(board_dir=str(Path(__file__).resolve().parent))
    with tempfile.TemporaryDirectory() as td:
        board = Path(td) / "board.md"
        board.write_text("HEAD\n")
        procs = [subprocess.Popen([sys.executable, "-c", body, str(board), tag, str(n)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for tag in ("A", "B")]
        errs = [p.communicate(timeout=120)[1].decode() for p in procs]
        check("race: beide Schreiber-Prozesse sauber beendet",
              all(p.returncode == 0 for p in procs) or print(errs) or False)
        lines = board.read_text().splitlines()
        got_a = sum(1 for x in lines if x.startswith("A-"))
        got_b = sum(1 for x in lines if x.startswith("B-"))
        check(f"race: kein Write verloren ({got_a}+{got_b} von {2 * n})",
              got_a == n and got_b == n)
        check("race: Kopfzeile überlebt (kein halb geschriebener Stand)",
              lines[0] == "HEAD")


def test_identitaets_guard_reklamiert_verwaiste_id() -> None:
    """Ein Hand-Edit, der die `@gc-id:`-Zeile mitnimmt, ist für alle Lost-Guards
    unsichtbar: die Zeilenbilanz stimmt, `lost_total` bleibt 0 — und `ensure_ids`
    stempelte bisher still eine neue ID, worauf Sub-Fäden und Sidecars ins Leere
    zeigten (real 2026-07-20 und 2026-08-06).

    Der Guard reklamiert die alte ID zurück, wenn eine junge Faden-Datei mit exakt dem
    Titel des ID-losen Items auf eine ID zeigt, die es nirgends mehr gibt. Getestet wird
    vor allem, wo er NICHT zugreift — ein falsch reklamierter Fremd-Faden wäre schlimmer
    als eine neue ID."""
    md = ("## T\n\n### Jetzt\n\n- [ ] Watchdog offline *(2026-08-06)*\n\n"
          "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    old_id, other_id = "d306b7cffdc8", "aaaabbbbcccc"

    def sidecar_file(d: Path, gc_id: str, title: str, age_days: float = 0) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{gc_id}-20260806-120000-a1b2.md"
        f.write_text(f"# Board-Agent-Antwort: {title}\n\n*egal*\n\ntext\n")
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(f, (t, t))
        return f

    def run(setup, archive_text: str = "") -> str:
        """Board frisch parsen, Sidecar-Lage aufbauen, ensure_ids laufen lassen → ID."""
        old_archive = server.BOARD_ARCHIVE
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "gc-threads"
            setup(d)
            arch = Path(td) / "board-archive.md"
            arch.write_text(archive_text)
            server.BOARD_ARCHIVE = arch
            try:
                b = server.parse_board(md)
                server.ensure_ids(b, d)
                return [it for _s, _n, _c, it in server._all_items(b)][0]["id"]
            finally:
                server.BOARD_ARCHIVE = old_archive

    check("id-guard: verwaiste ID mit passendem Titel wird zurückgeholt",
          run(lambda d: sidecar_file(d, old_id, "Watchdog offline")) == old_id)
    check("id-guard: anderer Titel → keine Reklamation, neue ID",
          run(lambda d: sidecar_file(d, old_id, "Ganz anderes Item")) != old_id)
    check("id-guard: Faden-Datei älter als 7 Tage gilt als Altlast, nicht als Verlust",
          run(lambda d: sidecar_file(d, old_id, "Watchdog offline", age_days=9)) != old_id)

    def zwei_kandidaten(d: Path) -> None:
        sidecar_file(d, old_id, "Watchdog offline")
        sidecar_file(d, other_id, "Watchdog offline")
    check("id-guard: zwei verwaiste IDs mit demselben Titel → mehrdeutig, keine Reklamation",
          run(zwei_kandidaten) not in (old_id, other_id))

    check("id-guard: ID steht im Archiv → Item ist abgehakt umgezogen, nicht verwaist",
          run(lambda d: sidecar_file(d, old_id, "Watchdog offline"),
              archive_text=f"- [x] Watchdog offline\n  @gc-id: {old_id}\n") != old_id)
    check("id-guard: fehlendes Sidecar-Verzeichnis kippt den Save nicht",
          len(run(lambda d: None)) == 12)
    check("id-guard: ohne reclaim_dir bleibt das alte Verhalten (reine Funktion)",
          len(_ensure_ids_plain(md)) == 12)


def _ensure_ids_plain(md: str) -> str:
    b = server.parse_board(md)
    server.ensure_ids(b)
    return [it for _s, _n, _c, it in server._all_items(b)][0]["id"]


def test_ritual_done_concurrent_no_duplicate() -> None:
    """Race-Condition-Regression (Sub-Review 22.07., mittlerer Schweregrad): ThreadingHTTPServer
    bedient Requests parallel — ohne RITUAL_LOCK könnten zwei fast gleichzeitige POSTs beide
    "noch kein done im Zyklus" sehen und je ein Event anhängen (Doppelklick am proof-losen
    Direktpfad, oder generell jedes Ritual). Feuert echte parallele Requests über echte Threads
    gegen den laufenden Test-Server, statt die Race nur zu behaupten."""
    from datetime import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        rfile, jfile = _ritual_env(Path(td))
        old_rfile, old_jfile, old_now = server.RITUALE_FILE, server.RITUAL_JOURNAL, server.ritual_now
        server.RITUALE_FILE, server.RITUAL_JOURNAL = rfile, jfile
        server.ritual_now = lambda: _dt(2026, 7, 22, 22, 0)
        fd, tmp = tempfile.mkstemp(suffix=".md")
        Path(tmp).write_text(SYNTH)
        httpd, port = _serve(Path(tmp))
        try:
            results = []
            def fire():
                results.append(_post(port, "/api/ritual-done", {"id": "nightcap", "proof": ""}))
            threads = [threading.Thread(target=fire) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            journal = jfile.read_text().strip().splitlines()
            check("ritual-done concurrent: alle 12 Requests kamen mit 200 durch",
                  len(results) == 12 and all(code == 200 for code, _ in results))
            check("ritual-done concurrent: trotz 12 gleichzeitiger POSTs genau 1 Journal-Event",
                  len(journal) == 1)
            done_flags = sum(1 for code, r in results if r.get("ok") is True and not r.get("already_done"))
            check("ritual-done concurrent: genau EIN Request gewinnt (ok ohne already_done)", done_flags == 1)
        finally:
            httpd.shutdown()
            Path(tmp).unlink(missing_ok=True)
            server.RITUALE_FILE, server.RITUAL_JOURNAL, server.ritual_now = old_rfile, old_jfile, old_now


def test_ritual_snooze_and_gate_override() -> None:
    """Snooze max 1× pro Ritual/Zyklus (409 beim zweiten Versuch); Gate-Override
    beruhigt das Gate für GATE_OVERRIDE_SILENCE_MIN (30 Min), danach wieder aktiv."""
    from datetime import datetime as _dt
    with tempfile.TemporaryDirectory() as td:
        rfile, jfile = _ritual_env(Path(td))
        old_rfile, old_jfile = server.RITUALE_FILE, server.RITUAL_JOURNAL
        old_now = server.ritual_now
        server.RITUALE_FILE, server.RITUAL_JOURNAL = rfile, jfile
        server.ritual_now = lambda: _dt(2026, 7, 22, 11, 30)  # Reflection ist überfällig
        fd, tmp = tempfile.mkstemp(suffix=".md")
        Path(tmp).write_text(SYNTH)
        httpd, port = _serve(Path(tmp))
        try:
            code, r = _post(port, "/api/ritual-snooze", {"id": "reflection"})
            check("snooze: 1. Versuch 200 + neue Deadline ~+1h",
                  code == 200 and r.get("snoozed_until", "").endswith("12:30"))
            code, r = _post(port, "/api/ritual-snooze", {"id": "reflection"})
            check("snooze: 2. Versuch selber Zyklus → 409", code == 409)
            st = next(r for r in server.rituale_status(server.ritual_now())
                      if r["id"] == "reflection")
            check("snooze: Status jetzt open (Deadline verschoben, noch nicht überfällig)",
                  st["status"] == "open" and st["snoozed_until"])

            check("gate: vor Override → nicht stumm", server.gate_silence_active(server.ritual_now()) is False)
            code, r = _post(port, "/api/gate-override", {"gate": "ritual"})
            check("gate-override: 200", code == 200 and r.get("ok") is True)
            check("gate: direkt danach → stumm (Silence aktiv)",
                  server.gate_silence_active(server.ritual_now()) is True)
            later = _dt(2026, 7, 22, 12, 5)  # +35 Min > GATE_OVERRIDE_SILENCE_MIN (30)
            check("gate: nach 35 Min → Silence abgelaufen", server.gate_silence_active(later) is False)
            code, r = _post(port, "/api/gate-override", {"gate": "unbekannt"})
            check("gate-override: falscher gate-Wert → 400", code == 400)
        finally:
            httpd.shutdown()
            Path(tmp).unlink(missing_ok=True)
            server.RITUALE_FILE, server.RITUAL_JOURNAL = old_rfile, old_jfile
            server.ritual_now = old_now


def test_on_field_roundtrip() -> None:
    """@on: (Termin-To-do, löst Item 54fe365c98e4) — Round-Trip verlustfrei, eigener
    lost-Guard, analog zu @wait."""
    txt = ("## Thema\n\n### Now\n\n"
           "- [ ] Follow-up Timur *(2026-07-10)*\n  @gc-id: dddddddddddd\n  @on: 2026-07-25\n\n"
           "### Next\n\n### Backlog\n\n# To discuss\n\n# Notes\n")
    b = server.parse_board(txt)
    it = b["themes"][0]["cols"]["Jetzt"][0]
    check("on: Feld geparst", it["on"] == "2026-07-25")
    check("on: Round-Trip verlustfrei", server.serialize_board(b).strip() == txt.strip())
    check("on: lost-Guard sauber", server.lost_total(txt, b) == 0)
    dup = txt.replace("  @on: 2026-07-25\n", "  @on: 2026-07-25\n  @on: 2026-08-01\n")
    check("on: doppeltes Feld blockt Save", server.lost_total(dup, server.parse_board(dup)) > 0)


def test_meeting_swimlane() -> None:
    """Personen-Tab = Besprechungsthemen-Board (06.08., Faden e84e15b8c6ba): ein
    Meeting-Eintrag trägt im md den 📅-Marker vor dem Namen und `kind: "meeting"` im
    Datenmodell — dieselbe `persons`-Liste, keine zweite. Round-Trip + Migration eines
    unmarkierten Alt-Eintrags dürfen nichts verlieren."""
    txt = ("## T\n\n### Now\n\n### Next\n\n### Backlog\n\n"
           "# To discuss\n\n"
           "## Anna → personal/people/anna.md\n\n"
           "- [ ] Gehaltsrunde *(2026-07-10)*\n  @gc-id: dddddddddddd\n\n"
           "## 📅 Mittwoch Domain Alignment\n\n"
           "- [ ] tbc: WhatsApp *(2026-07-27)*\n  @gc-id: eeeeeeeeeeee\n\n"
           "# Notes\n")
    b = server.parse_board(txt)
    check("meeting: 2 Personen-Einträge geparst", len(b["persons"]) == 2)
    anna, mittwoch = b["persons"]
    check("meeting: Person ohne Marker bleibt kind-los", "kind" not in anna)
    check("meeting: 📅-Marker aus dem Namen entfernt", mittwoch["name"] == "Mittwoch Domain Alignment")
    check("meeting: kind=meeting gesetzt", mittwoch.get("kind") == "meeting")
    check("meeting: Items bleiben unter derselben persons-Liste (kein 2. Array)",
          len(mittwoch["items"]) == 1 and mittwoch["items"][0]["id"] == "eeeeeeeeeeee")
    check("meeting: Round-Trip verlustfrei", server.serialize_board(b).strip() == txt.strip())
    check("meeting: lost-Guard sauber", server.lost_total(txt, b) == 0)
    check("meeting: parse→serialize→parse strukturgleich", server.parse_board(server.serialize_board(b)) == b)

    unmarked = txt.replace("## 📅 Mittwoch Domain Alignment", "## Mittwoch Domain Alignment")
    ub = server.parse_board(unmarked)
    check("meeting: ohne Marker fällt auf kind=person zurück (Migrations-Fall)",
          "kind" not in ub["persons"][1])


def test_wesen_hungry_precedence() -> None:
    """hungry (Ritual überfällig) hat Vorrang VOR burst — auch wenn die Board-Last
    allein schon burst auslösen würde (Spec: 'Vorrang VOR burst')."""
    from datetime import date as _date
    today = _date(2026, 7, 21)
    noarch = Path("/nonexistent")
    many = "\n".join(f"- [ ] Item {i} *(2026-07-01)*" for i in range(16))  # würde allein "burst" sein
    board = server.parse_board(f"## Thema\n\n### Jetzt\n\n{many}\n### Bald\n\n### Geparkt\n")
    baseline = server.wesen_status(board, today, noarch)
    check("wesen: ohne Ritual-Info bleibt es beim Board-Zustand (burst)", baseline["state"] == "burst")
    overdue_rituale = [{"id": "reflection", "title": "Reflection ✍️", "status": "overdue"},
                        {"id": "social", "title": "Social-Kontakt", "status": "open"}]
    w = server.wesen_status(board, today, noarch, rituale=overdue_rituale, gate_silenced=False)
    check("wesen: Ritual überfällig → hungry schlägt burst", w["state"] == "hungry"
          and "Reflection" in w["why"])
    w2 = server.wesen_status(board, today, noarch, rituale=overdue_rituale, gate_silenced=True)
    check("wesen: Gate-Silence aktiv → hungry greift nicht, zurück auf burst", w2["state"] == "burst")


def test_wesen_trend() -> None:
    """Verlaufs-Dekoration der Warum-Zeile (30.07.: „bleibt es länger da? addiert sich
    das auf?"). Zwei Signale aus wesen-history.jsonl — Tage-in-Folge + Bewegung der
    Jetzt-Menge über 7 Tage. Darf den Zustand nie ändern und nie werfen."""
    from datetime import date as _date
    today = _date(2026, 7, 21)
    hist = server.WESEN_HISTORY
    hist.parent.mkdir(parents=True, exist_ok=True)
    alt = hist.read_text(encoding="utf-8") if hist.is_file() else None
    try:
        hist.write_text("\n".join(json.dumps(r) for r in [
            {"date": "2026-07-16", "state": "ache", "jetzt": 12},
            {"date": "2026-07-18", "state": "burst", "jetzt": 14},
            {"date": "2026-07-19", "state": "burst", "jetzt": 15},
            {"date": "2026-07-20", "state": "burst", "jetzt": 15},
        ]) + "\n", encoding="utf-8")
        check("wesen-trend: Tage in Folge + Zuwachs gegen den 7-Tage-Anfang",
              server._wesen_trend("burst", 18, today) == " · DAY 4 · NOW ↑6 (7D)")
        check("wesen-trend: anderer Zustand → kein Streak, nur die Bewegung",
              server._wesen_trend("healthy", 10, today) == " · NOW ↓2 (7D)")
        hist.write_text("kaputt{\n", encoding="utf-8")
        check("wesen-trend: unlesbare Historie bleibt folgenlos",
              server._wesen_trend("burst", 18, today) == "")
    finally:
        if alt is None:
            hist.unlink(missing_ok=True)
        else:
            hist.write_text(alt, encoding="utf-8")


HIER = """## Thema

### Now

- [ ] Elternthema *(2026-07-23)*
  @gc-id: p00000000000
  @gc: bau das banner
  @gc-re: erster stand
  @gc: und jetzt weiter

- [ ] Sub A *(2026-07-23)*
  @gc-id: c00000000001
  @gc-parent: p00000000000
  @gc: kim fragen
  @gc-re: kim hat geantwortet — flag steht

- [ ] Sub B *(2026-07-23)*
  @gc-id: c00000000002
  @gc-parent: p00000000000

- [ ] Fremdes Item *(2026-07-23)*
  @gc-id: x00000000003

### Next

### Backlog

# To discuss

# Notes
"""


def test_hierarchy_datamodel() -> None:
    """Hierarchische Items (Design abgenommen 2026-07-23): flache Items + @gc-parent.
    Round-Trip, lost-Guard, Tiefen-/Zyklen-Guard, sys-Turn-Neutralität."""
    b = server.parse_board(HIER)
    check("hier: round-trip wortgleich", server.serialize_board(b) == HIER)
    idx = server.item_index(b)
    kids = server.children_of(b, "p00000000000", idx)
    check("hier: 2 Subs am Eltern-Item", [k["id"] for k in kids] == ["c00000000001", "c00000000002"])
    check("hier: Fremd-Item ist kein Sub", server.parent_of(idx["x00000000003"], idx) is None)
    check("hier: lost_total sauber", server.lost_total(HIER, b) == 0)
    # Zweite @gc-parent-Zeile am selben Item → Guard blockt (wie bei @gc-id)
    doppelt = HIER.replace("  @gc-parent: p00000000000\n  @gc: kim fragen",
                           "  @gc-parent: p00000000000\n  @gc-parent: x00000000003\n  @gc: kim fragen")
    check("hier: doppelter Zeiger wird als Verlust erkannt",
          server.lost_parent_lines(doppelt, server.parse_board(doppelt)) == 1)
    # Tiefen-/Zyklen-Guard: Eltern trägt selbst einen Zeiger → Kante wirkungslos
    zyklus = server.parse_board(HIER.replace("  @gc-id: p00000000000",
                                             "  @gc-id: p00000000000\n  @gc-parent: c00000000001"))
    zidx = server.item_index(zyklus)
    check("hier: A→B→A ist keine gültige Kante",
          server.parent_of(zidx["c00000000001"], zidx) is None
          and server.parent_of(zidx["p00000000000"], zidx) is None)
    selbst = server.parse_board(HIER.replace("@gc-parent: p00000000000\n  @gc: kim",
                                             "@gc-parent: c00000000001\n  @gc: kim"))
    sidx = server.item_index(selbst)
    check("hier: Selbstreferenz zählt nicht", server.parent_of(sidx["c00000000001"], sidx) is None)
    # sys-Turn: eigener Event-Typ, kippt thread_status NICHT (Sol-Befund 1)
    par = idx["p00000000000"]
    check("hier: Eltern wartet auf GC", server.thread_status(par) == "for_gc")
    par["thread"].append({"kind": "sys", "text": "✓ Sub erledigt: Sub A [sub:c00000000001] · x *(2026-07-23 20:00)*"})
    check("hier: sys-Turn lässt thread_status unberührt", server.thread_status(par) == "for_gc")
    txt = server.serialize_board(b)
    check("hier: sys-Turn serialisiert als @gc-sys:", "  @gc-sys: ✓ Sub erledigt: Sub A" in txt)
    check("hier: sys-Turn überlebt Re-Parse",
          server.parse_board(txt)["themes"][0]["cols"]["Jetzt"][0]["thread"][-1]["kind"] == "sys")
    check("hier: sys-Turn im lost-Guard mitgezählt", server.lost_total(txt, server.parse_board(txt)) == 0)


def test_hierarchy_rollup() -> None:
    """Roll-up erledigter Subs: EIN idempotenter Handler, keyed by Child-ID; Status und
    Ergebnis getrennt (kein erfundener Text, wenn von Hand abgehakt) — Sol-Befund 3."""
    b = server.parse_board(HIER)
    idx = server.item_index(b)
    idx["c00000000001"]["done"] = True   # Sub mit Antwort
    idx["c00000000002"]["done"] = True   # Sub ohne jeden Faden
    n = server.rollup_child_completions(b)
    sysline = [e["text"] for e in idx["p00000000000"]["thread"] if e["kind"] == "sys"]
    check("rollup: eine Zeile je erledigtem Sub", n == 2 and len(sysline) == 2)
    check("rollup: Ergebnis = letzte Antwort des Subs", "kim hat geantwortet" in sysline[0])
    check("rollup: Marker trägt die Child-ID", "[sub:c00000000001]" in sysline[0])
    check("rollup: ohne Ergebnistext keine Erfindung", "checked off manually" in sysline[1])
    check("rollup: idempotent", server.rollup_child_completions(b) == 0)
    # Reopen: Zeile bleibt stehen (append-only), der Fortschritt kommt aus dem Ist-Zustand
    idx["c00000000001"]["done"] = False
    check("rollup: reopen erzeugt keine zweite Zeile", server.rollup_child_completions(b) == 0)
    check("rollup: Elternitem wird NIE automatisch abgehakt", not idx["p00000000000"]["done"])


def test_hierarchy_spawn_endpoint() -> None:
    """/api/gc-spawn-sub: Server bleibt Single-Writer beim Abspalten. Sub landet direkt
    hinter dem Eltern-Item, Tiefen-Guard greift, unbekannte ID → 404."""
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(HIER)
    httpd, port = _serve(Path(tmp))
    try:
        code, r = _post(port, "/api/gc-spawn-sub",
                        {"parent_id": "p00000000000", "title": "Sub C", "ask": "s3 bucket anlegen"})
        after = server.parse_board(Path(tmp).read_text())
        col = after["themes"][0]["cols"]["Jetzt"]
        check("spawn: 200 + neue id", code == 200 and len(r.get("id", "")) == 12)
        check("spawn: Sub steht direkt hinter dem Eltern-Item", col[1]["title"] == "Sub C")
        check("spawn: Zeiger + erster @gc:-Turn gesetzt",
              col[1]["parent"] == "p00000000000" and col[1]["thread"][0]["kind"] == "ask")
        check("spawn: wartet sofort auf GC", server.thread_status(col[1]) == "for_gc")
        code2, r2 = _post(port, "/api/gc-spawn-sub", {"parent_id": r["id"], "title": "Sub-Sub"})
        check("spawn: nur eine Ebene (409)", code2 == 409 and "one level" in r2.get("error", ""))
        code3, _ = _post(port, "/api/gc-spawn-sub", {"parent_id": "deadbeef0000", "title": "X"})
        check("spawn: unbekannte Eltern-ID → 404", code3 == 404)
        code4, _ = _post(port, "/api/gc-spawn-sub", {"parent_id": "p00000000000", "title": "  "})
        check("spawn: leerer Titel → 400", code4 == 400)
        # Autor-Kopf (System Review 02.09., Faden bab941e50135): Agenten-Briefs bekommen
        # `# Agent brief:`, das Feld des Owners weiterhin `# <Owner> turn:` — derselbe Endpoint.
        import sidecar
        long_ask = "Build brief: " + ("x" * 600)
        sc_dir = Path(tmp).parent / "gc-threads"
        code5, r5 = _post(port, "/api/gc-spawn-sub",
                          {"parent_id": "p00000000000", "title": "Sub agent", "ask": long_ask, "by": "agent"})
        code6, r6 = _post(port, "/api/gc-spawn-sub",
                          {"parent_id": "p00000000000", "title": "Sub human", "ask": long_ask})
        heads = {}
        for rid in (r5.get("id"), r6.get("id")):
            for f in sc_dir.glob(f"{rid}-*.md"):
                heads[rid] = f.read_text().split("\n", 1)[0]; f.unlink()
        check("spawn: by=agent → Sidecar-Kopf 'Agent brief'",
              code5 == 200 and heads.get(r5.get("id"), "").startswith("# Agent brief:"))
        check("spawn: ohne by → Sidecar-Kopf '<Owner> turn'",
              code6 == 200 and heads.get(r6.get("id"), "")
              .startswith(f"# {sidecar.HEADER_LABEL['ask']}:"))
        col2 = server.parse_board(Path(tmp).read_text())["themes"][0]["cols"]["Jetzt"]
        check("spawn: by=agent → Faden-Turn bleibt kind=ask mit Verweis 'voller Text'",
              any(x["title"] == "Sub agent" and x["thread"][0]["kind"] == "ask"
                  and "full text: " in x["thread"][0]["text"]   # Sidecar-Dir liegt im Test außerhalb des Repos
                  and "gc-threads/" in x["thread"][0]["text"] for x in col2))
    finally:
        httpd.shutdown(); os.close(fd); os.unlink(tmp)


def test_hierarchy_prompt_context() -> None:
    """Kontext runter (Eltern-ID + letzte 3 Turns) und hoch (Sub-Status bei JEDEM
    Eltern-Turn, auch beim Resume) — der eigentliche Mehrwert gegenüber Jira."""
    b = server.parse_board(HIER)
    idx = server.item_index(b)
    child = server.pending_entry("theme", "Thema", "Jetzt", idx["c00000000001"], b)
    p = gc_runner.build_prompt(child, resume=False)
    check("kontext-runter: Eltern-Titel + ID im Prompt",
          "Elternthema" in p and "p00000000000" in p)
    check("kontext-runter: Eltern-Turns im Prompt", "bau das banner" in p and "erster stand" in p)
    check("kontext-runter: Ausschnitt ehrlich benannt", "EXCERPT" in p and "inbox/board.md" in p)
    check("kontext-runter: Sub bekommt keinen Spawn-Hinweis", "gc-spawn-sub" not in p)
    parent = server.pending_entry("theme", "Thema", "Jetzt", idx["p00000000000"], b)
    pp = gc_runner.build_prompt(parent, resume=False)
    check("kontext-hoch: Sub-Liste im Eltern-Prompt", "SUB-THREADS" in pp and "Sub A" in pp and "Sub B" in pp)
    check("kontext-hoch: Spawn-Hinweis mit echter Eltern-ID",
          "gc-spawn-sub" in pp and '"parent_id":"p00000000000"' in pp)
    pr = gc_runner.build_prompt(parent, resume=True)
    check("kontext-hoch: Sub-Status geht AUCH beim Resume mit", "SUB-THREADS" in pr)
    check("kontext-hoch: Spawn-Hinweis beim Resume gespart (Kontrakt-Diät)", "gc-spawn-sub" not in pr)
    # Alle Subs erledigt → Nudge statt Auto-Abhaken
    idx["c00000000001"]["done"] = idx["c00000000002"]["done"] = True
    done_parent = server.pending_entry("theme", "Thema", "Jetzt", idx["p00000000000"], b)
    pd = gc_runner.build_prompt(done_parent, resume=True)
    check("kontext-hoch: Nudge bei 2/2", "2/2 done" in pd and "do not check it off yourself" in pd)
    # Ohne board-Argument (Alt-Aufrufer) bleibt alles leer — reines Add-on
    plain = server.pending_entry("theme", "Thema", "Jetzt", idx["c00000000001"])
    check("kontext: ohne board kein Hierarchie-Block", plain["hierarchy"] == {}
          and "SUB-THREADS" not in gc_runner.build_prompt(plain, resume=False))


def test_hierarchy_sweep_pairing() -> None:
    """Eltern und Subs nur GEMEINSAM archivieren (Sol-Befund 2) — sonst bleiben verwaiste
    @gc-parent-Zeiger zurück und der Subs-Zähler zeigt ins Leere."""
    import sys as _sys

    import sweep
    reif = "*(2020-01-01)*"
    txt = ("## T\n\n### Jetzt\n\n"
           f"- [x] Eltern {reif}\n  @gc-id: p00000000000\n\n"
           f"- [x] Sub reif {reif}\n  @gc-id: c00000000001\n  @gc-parent: p00000000000\n\n"
           "- [ ] Sub offen *(2020-01-01)*\n  @gc-id: c00000000002\n  @gc-parent: p00000000000\n\n"
           f"- [x] Einzelitem {reif}\n  @gc-id: x00000000003\n\n"
           "### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    with tempfile.TemporaryDirectory() as td:
        board_f, arch_f = Path(td) / "board.md", Path(td) / "board-archive.md"
        board_f.write_text(txt)
        old = sweep.BOARD, sweep.ARCHIVE, _sys.argv
        try:
            sweep.BOARD, sweep.ARCHIVE, _sys.argv = board_f, arch_f, ["sweep.py"]
            sweep.main()
        finally:
            sweep.BOARD, sweep.ARCHIVE, _sys.argv = old
        after = board_f.read_text()
        check("sweep-paarung: Familie bleibt zusammen im Board",
              "Eltern" in after and "Sub reif" in after and "Sub offen" in after)
        check("sweep-paarung: unbeteiligtes Item wird normal archiviert",
              "Einzelitem" not in after and arch_f.exists() and "Einzelitem" in arch_f.read_text())
        # Jetzt ist die ganze Familie reif → alles zusammen ins Archiv
        board_f.write_text(after.replace("- [ ] Sub offen", "- [x] Sub offen"))
        try:
            sweep.BOARD, sweep.ARCHIVE, _sys.argv = board_f, arch_f, ["sweep.py"]
            sweep.main()
        finally:
            sweep.BOARD, sweep.ARCHIVE, _sys.argv = old
        after2, arch = board_f.read_text(), arch_f.read_text()
        check("sweep-paarung: reife Familie wandert gemeinsam",
              "Eltern" not in after2 and "Sub reif" not in after2 and "Sub offen" not in after2
              and "Eltern" in arch and "Sub reif" in arch and "Sub offen" in arch)


def test_zzz_alle_checks_gruen() -> None:
    """MUSS die letzte test_-Funktion der Datei bleiben (pytest läuft in Definitions-
    reihenfolge). Grund: `check()` sammelt nur in FAILS und wirft nicht — unter pytest
    meldete die Suite deshalb GRÜN, obwohl einzelne Checks rot waren (aufgefallen
    21.07.: eine kaputte wesen-Zusicherung lief 41× „passed" mit). Der Skriptlauf
    `python3 test_server.py` wertet FAILS selbst aus; dieser Test zieht pytest nach."""
    assert not FAILS, f"{len(FAILS)} Check(s) fehlgeschlagen: {FAILS}"


def test_git_kontext_im_prompt() -> None:
    """Der Git-Kontext gehört in den PROMPT, nicht in den System-Prompt.

    Hintergrund (2026-07-28, Item 82a909da0c31): Claude Code baut seinen Git-Status-Block
    bei jedem Prozessstart neu — auch beim --resume — und bricht damit den Prompt-Cache jedes
    Folge-Runs. Wir schalten ihn per RUN_ENV ab und hängen die Fakten selbst an den Prompt.
    Was hier geschützt wird: der Schalter ist wirklich gesetzt, beide Prompt-Zweige tragen den
    Block, und ein Folge-Run bekommt das Delta statt des vollen Schnappschusses.
    """
    check("git: nativer Block ist für Board-Runs abgeschaltet",
          gc_runner.RUN_ENV.get("CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS") == "1")
    check("git: Co-Authored-By-Trailer im Kontrakt nachgebaut (fällt mit dem Schalter weg)",
          "Co-Authored-By: Claude <noreply@anthropic.com>" in gc_runner.PROMPT_CONTRACT)

    pending = {"addr": {"id": "abcabcabcabc", "name": "Dev", "col": "Jetzt"},
               "title": "Testitem", "body": [], "thread": [{"kind": "ask", "text": "tu was"}],
               "session": "", "gc_last": ""}
    with tempfile.TemporaryDirectory() as td:
        anchor_before = gc_runner.GIT_ANCHOR
        gc_runner.GIT_ANCHOR = Path(td) / "git-anchor.json"
        try:
            fresh = gc_runner.build_prompt(pending, resume=False)
            check("git: frischer Run bekommt den Schnappschuss", "## Git" in fresh
                  and "Branch:" in fresh)
            # Ohne Anker (erster Folge-Run nach dem Deploy) muss der volle Schnappschuss kommen,
            # nicht ein leeres Delta — sonst startet der Agent blind.
            res_kalt = gc_runner.build_prompt(pending, resume=True)
            check("git: Resume ohne Anker fällt auf den Schnappschuss zurück",
                  "Branch:" in res_kalt)

            gc_runner._anchor_save("abcabcabcabc", {"head": "deadbeef", "dirty": []})
            saved = json.loads(gc_runner.GIT_ANCHOR.read_text())
            check("git: Anker wird pro Item abgelegt", "abcabcabcabc" in saved
                  and saved["abcabcabcabc"]["head"] == "deadbeef")
            # Anker mit unbekanntem SHA → git_delta liefert {} → kein Delta, aber auch kein Crash.
            res = gc_runner.build_prompt(pending, resume=True)
            check("git: Resume mit Anker crasht nicht und bleibt kurz",
                  "## Git" in res and len(res) < len(fresh) + 400)

            # Der Block darf den Prompt-ANFANG nicht anfassen — genau daran hängt der Cache.
            check("git: Block hängt hinten an (Prompt-Anfang unverändert)",
                  fresh.index("## Git") > len(fresh) * 0.5)
        finally:
            gc_runner.GIT_ANCHOR = anchor_before


def test_netcheck_bewertung() -> None:
    """Die Bewertungslogik des Verbindungs-Checks — hermetisch, ohne echtes Netz.
    Wichtig ist die Trennung der beiden Messungen: „Leitung tot" und „Modell zäh,
    Leitung ok" sind verschiedene Befunde, und ein Zertifikatsfehler beim Statusabruf
    ist gar keiner (lokales Python-Problem, s. netcheck-Kommentar)."""
    tcp_before, http_before = server._tcp_probe, server._http_probe

    def lauf(tcp: dict, http: dict) -> dict:
        server._tcp_probe = lambda h, p: tcp
        server._http_probe = lambda u: http
        return server.netcheck(force=True)

    def status(indicator: str, desc: str = "") -> dict:
        return {"ok": True, "ms": 200,
                "body": json.dumps({"status": {"indicator": indicator, "description": desc}})}

    try:
        r = lauf({"ok": True, "ms": 30}, status("none"))
        check("netcheck: alles gut → keine Meldung", r["level"] == "" and r["msg"] == "")

        r = lauf({"ok": False, "ms": 5000, "error": "timeout"}, status("none"))
        check("netcheck: kein Netz → bad", r["level"] == "bad" and "This Mac is offline" in r["msg"])

        r = lauf({"ok": True, "ms": server.NETCHECK_SLOW_MS + 1}, status("none"))
        check("netcheck: langsame Leitung → warn", r["level"] == "warn")

        r = lauf({"ok": True, "ms": server.NETCHECK_DEAD_MS + 1}, status("none"))
        check("netcheck: sehr langsame Leitung → bad", r["level"] == "bad")

        # Genau der Fall, den der Owner ergänzt hat: Leitung tadellos, Anthropic klemmt.
        r = lauf({"ok": True, "ms": 30}, status("major", "Elevated errors"))
        check("netcheck: Anthropic-Störung bei guter Leitung → bad",
              r["level"] == "bad" and "Anthropic" in r["msg"] and r["indicator"] == "major")

        r = lauf({"ok": True, "ms": 30}, {"ok": False, "ms": 400, "error": "URLError", "cert": True})
        check("netcheck: Zertifikatsfehler ist KEINE Störung", r["level"] == "")

        r = lauf({"ok": True, "ms": 30}, {"ok": False, "ms": 400, "error": "URLError", "cert": False})
        check("netcheck: Statusseite unerreichbar bei gutem Netz → warn", r["level"] == "warn")

        # Cache: zweiter Aufruf ohne force darf NICHT erneut messen (die UI fragt oft).
        server._tcp_probe = lambda h, p: {"ok": False, "ms": 5000, "error": "timeout"}
        check("netcheck: Ergebnis wird gecacht", server.netcheck()["level"] == "warn")
    finally:
        server._tcp_probe, server._http_probe = tcp_before, http_before
        server._NET_CACHE.update(ts=0.0, data=None)


def test_index_torso_wird_nie_ausgeliefert() -> None:
    """Ein GET auf `/` darf niemals eine halb geschriebene index.html zurückgeben.

    Herkunft (06.08.): beim Nachgehen einer „halb gerenderten UI" gefunden — die Ursache war
    dort eine andere (Ritual-Gate), die Lücke hier ist trotzdem echt. index.html wird im
    laufenden Betrieb nicht-atomar überschrieben (Agenten-Editor, `bump.py`: truncate + write);
    ein Request in genau diesem Fenster bekam einen Torso, und der Tab bleibt kaputt, bis
    jemand neu lädt. Geprüft wird die Eigenschaft, auf die es ankommt: unvollständig → lieber
    die letzte bekannt-gute Fassung."""
    orig_root = server.ROOT
    voll = (orig_root / "index.html").read_bytes()
    server.read_index_html()                       # letzte gute Fassung wärmen
    d = Path(tempfile.mkdtemp())
    try:
        (d / "index.html").write_bytes(voll[:5000])   # Torso mitten im Schreiben
        server.ROOT = d
        out = server.read_index_html()
        check("index-torso: halbe Datei wird nicht ausgeliefert", out.rstrip().endswith(b"</html>"))
        check("index-torso: stattdessen die letzte gute Fassung", out == voll)
        (d / "index.html").write_bytes(voll)          # Schreiben fertig
        check("index-torso: vollständige Datei geht wieder normal raus",
              server.read_index_html() == voll)
    finally:
        server.ROOT = orig_root


def test_live_pfade_umgebogen() -> None:
    """Die Umbiegungen am Dateikopf sind die einzige Bremse zwischen Suite und den
    echten Daten des Owners — und sie wurde inzwischen VIER Mal einzeln vergessen (Journal,
    Usage-Log, Wesen-Historie, Receipts, Kill-Log). Jedes Mal fiel es erst auf, als
    Fantasie-Daten im Board standen. Dieser Test dreht das um: wer künftig einen neuen
    Schreibpfad einbaut und die Umbiegung vergisst, sieht es hier — nicht der Owner im Banner.

    Geprüft wird die Eigenschaft, auf die es ankommt (liegt im Temp, nicht im Repo), nicht ein
    konkreter Pfadname; sonst müsste der Test bei jeder Umbenennung mitgepflegt werden."""
    tmp_root = Path(tempfile.gettempdir()).resolve()
    for name, p in (("JOURNAL_DIR", gc_runner.JOURNAL_DIR), ("USAGE_LOG", gc_runner.USAGE_LOG),
                    ("KILL_LOG", gc_runner.KILL_LOG), ("WESEN_HISTORY", server.WESEN_HISTORY),
                    ("RECEIPT_DIR", receipt.RECEIPT_DIR)):
        check(f"live-pfade: {name} liegt im Temp, nicht in den Echtdaten",
              tmp_root in Path(p).resolve().parents)


def test_blatt_am_faden_nur_letzter_turn() -> None:
    """Entscheidungsblatt-Erkennung (2026-08-11, Blatt Q3=A): ein .html-Pfad zählt
    NUR im letzten Faden-Turn — „wenn es vor 2 turns war, soll es nicht gezeigt werden".
    Damit verschwindet das Zeichen von selbst, sobald der Owner geantwortet hat. Lange
    Turns stehen in board.md nur als Sidecar-Verweis: dann muss der Volltext gelesen
    werden, sonst sieht der Server ausgerechnet bei den Agent-Antworten nichts."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()   # macOS: /var -> /private/var, sonst schlaegt das Containment fehl
        (root / "tmp" / "entscheidungen").mkdir(parents=True)
        blatt = "tmp/entscheidungen/probe-entscheidung.html"
        (root / blatt).write_text("<html></html>")
        threads = root / "inbox" / "gc-threads"
        threads.mkdir(parents=True)
        (threads / "aaaa-1.md").write_text(f"Blatt liegt offen: `{blatt}` — vier Fragen.")
        orig_root, orig_dir = server.GC_ROOT, server.sidecar.SIDECAR_DIR
        try:
            server.GC_ROOT = root
            server.sidecar.SIDECAR_DIR = threads

            inline = {"thread": [{"kind": "ask", "text": "Frage"},
                                 {"kind": "reply", "text": f"Blatt: {blatt}"}]}
            check("blatt: Pfad im letzten Turn wird gefunden",
                  server.item_sheet(inline) == blatt)

            veraltet = {"thread": [{"kind": "reply", "text": f"Blatt: {blatt}"},
                                   {"kind": "ask", "text": "Danke, hier meine Antworten"}]}
            check("blatt: zwei Turns alt zaehlt NICHT",
                  server.item_sheet(veraltet) == "")

            sidecar_turn = {"thread": [{"kind": "reply",
                                        "text": "Studie fertig … → volle Antwort: inbox/gc-threads/aaaa-1.md"}]}
            check("blatt: Pfad aus dem Sidecar-Volltext wird gefunden",
                  server.item_sheet(sidecar_turn) == blatt)

            # Regression (Faden 2ddd73779387, 14.08.): die Denial-Warnung wurde HINTER den
            # Sidecar-Verweis gehaengt und brach damit dessen Zeilenende-Anker — kein
            # Volltext, kein Blatt, und der Owner sah nur den Kurzsatz. Die Warnung gehoert
            # vor den Verweis; hier bewusst ueber den echten Runner-Pfad gebaut.
            mit_denial = {"thread": [{"kind": "reply", "text": gc_runner._with_denial_note(
                "Studie fertig … → volle Antwort: inbox/gc-threads/aaaa-1.md", 2)}]}
            check("blatt: Denial-Warnung schiebt den Sidecar-Verweis nicht vom Zeilenende",
                  server.item_sheet(mit_denial) == blatt)

            # Regression: ein ABSOLUT verlinktes Blatt matcht SHEET_RE nie (Lookbehind
            # blockt jeden Start hinter einem "/") und war damit unsichtbar. Absolute
            # Pfade unters Repo-Root werden jetzt relativiert.
            absolut = {"thread": [{"kind": "reply", "text": f"Blatt: {root}/{blatt} — vier Fragen."}]}
            check("blatt: absoluter Pfad unters Repo-Root wird relativiert",
                  server.item_sheet(absolut) == blatt)

            weg = {"thread": [{"kind": "reply", "text": "Blatt: tmp/entscheidungen/geloescht.html"}]}
            check("blatt: nicht existierende Datei liefert nichts", server.item_sheet(weg) == "")

            traversal = {"thread": [{"kind": "reply", "text": "Blatt: ../../etc/x.html"}]}
            check("blatt: Traversal-Pfad liefert nichts", server.item_sheet(traversal) == "")

            check("blatt: Item ohne Faden liefert nichts", server.item_sheet({}) == "")

            # A demo (click-through) renders in the same split pane but asks NOTHING —
            # the card must not flip to "waiting on the owner" because of it.
            (root / "docs" / "demos").mkdir(parents=True)
            demo = "docs/demos/radar.html"
            (root / demo).write_text("<html></html>")
            mit_demo = {"thread": [{"kind": "reply", "text": f"Built and tested: {demo}"}]}
            check("demo: wird als Blatt-Pfad gefunden (gleiche Andockstelle)",
                  server.item_sheet(mit_demo) == demo)
            check("demo: gilt als demo, nicht als sheet",
                  server.sheet_kind(demo) == "demo" and server.sheet_kind(blatt) == "sheet")
            check("demo: setzt die Karte NICHT auf needs_input",
                  server.item_needs_input(mit_demo) == "")

            # One turn may name a click-through, a standalone visual report and a
            # publishing payload. The reader should get the report — not whichever
            # .html happened to come last, and never a raw markup fragment.
            project = root / "projects" / "review"
            project.mkdir(parents=True)
            overview = "projects/review/overview.html"
            storage = "projects/review/publish-payload.html"
            (root / overview).write_text("<!doctype html><html><body>Portfolio</body></html>")
            (root / storage).write_text("<ac:structured-macro><ri:page /></ac:structured-macro>")
            mixed = {"thread": [{"kind": "reply", "text":
                f"Demo: {demo}\nOverview: {overview}\nPublish payload: {storage}"}]}
            check("artifact: visueller Report gewinnt vor Demo und Publish-Payload",
                  server.item_sheet(mixed) == overview)
            check("artifact: eigener Typ und kein needs_input",
                  server.sheet_kind(overview) == "artifact"
                  and server.item_needs_input(mixed) == "")
            payload_only = {"thread": [{"kind": "reply", "text": f"Payload: {storage}"}]}
            check("artifact: Publish-Payload wird nie automatisch angedockt",
                  server.item_sheet(payload_only) == "")
        finally:
            server.GC_ROOT, server.sidecar.SIDECAR_DIR = orig_root, orig_dir


def test_abhaken_ist_der_fadenschnitt() -> None:
    """Abhaken: der Kartenzustand ist abgeleitet, nicht gespeichert — wartet die letzte
    Antwort auf den Haken, zeigt der Knopf ✓ statt ▶. Der Haken IST der `done`-Turn, den
    auch `✂ New thread` schreibt; deshalb muss das Prädikat exakt die Gegenprobe zu
    gc_runner.session_cut sein: „abgehakt" heißt zugleich „nächster Run startet frisch"."""
    import gc_runner
    nie = {"thread": []}
    check("abhaken: nie gelaufen → nichts abzuhaken", server.item_awaiting_cut(nie) is False)
    offen = {"thread": [{"kind": "ask", "text": "▶ Run Dreaming"},
                        {"kind": "reply", "text": "Ledger kuratiert."}]}
    check("abhaken: Antwort ohne Schnitt → wartet auf den Haken",
          server.item_awaiting_cut(offen) is True)
    gehakt = {"thread": offen["thread"] + [{"kind": "done", "text": ""}]}
    check("abhaken: nach dem Schnitt wieder ▶", server.item_awaiting_cut(gehakt) is False)
    weiter = {"thread": gehakt["thread"] + [{"kind": "ask", "text": "und noch das"}]}
    check("abhaken: eigener Turn nach dem Schnitt hält ▶ frei",
          server.item_awaiting_cut(weiter) is False)
    neu = {"thread": weiter["thread"] + [{"kind": "reply", "text": "gemacht."}]}
    check("abhaken: neue Antwort wartet wieder", server.item_awaiting_cut(neu) is True)
    nur_sys = {"thread": [{"kind": "ask", "text": "▶ Run"}, {"kind": "sys", "text": "crash"}]}
    check("abhaken: Lauf ohne Antwort ist nichts abzuhaken",
          server.item_awaiting_cut(nur_sys) is False)
    for name, it in (("offen", offen), ("gehakt", gehakt), ("weiter", weiter), ("neu", neu)):
        check(f"abhaken: Gegenprobe zu session_cut ({name})",
              server.item_awaiting_cut(it) is not gc_runner.session_cut(it["thread"]))
    board = {"themes": [], "persons": [], "cockpit": [dict(offen, title="Dreaming", body=["action:dreaming"], done=False,
                              subs=[], id="abc123", date="2026-08-18")]}
    server.annotate_sheets(board)
    check("abhaken: /api/board trägt awaiting_cut als Anzeige-Feld",
          board["cockpit"][0]["awaiting_cut"] is True)
    check("abhaken: das Feld ist kein board.md-Inhalt",
          "awaiting_cut" not in "\n".join(server.item_lines(board["cockpit"][0])))


def test_needs_input_prädikat_und_carryover() -> None:
    """Needs-Input (17.08., Blatt auto-run-needs-input): drei Signale am letzten
    Nicht-sys-Turn (Blatt / 🔑 CLI-Handoff / ❓-Frage), selbstlöschend sobald der Owner
    antwortet. Q3=C „nur warnen, nie blocken": startet trotzdem ein Run, trägt der Prompt
    den Carry-over-Auftrag — die offene Entscheidung darf nie stillschweigend verschwinden."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        (root / "tmp" / "entscheidungen").mkdir(parents=True)
        blatt = "tmp/entscheidungen/probe-entscheidung.html"
        (root / blatt).write_text("<html></html>")
        orig_root = server.GC_ROOT
        try:
            server.GC_ROOT = root
            mit_blatt = {"thread": [{"kind": "reply", "text": f"Vier Fragen: {blatt}"}]}
            check("needs_input: Blatt am letzten Turn → sheet",
                  server.item_needs_input(mit_blatt) == "sheet")
            handoff = {"thread": [{"kind": "reply", "text": "🔑 CLI-Handoff needed: interactive login\n\n```sh\nservice-login\n```"}]}
            check("needs_input: 🔑-Handoff → handoff",
                  server.item_needs_input(handoff) == "handoff")
            frage = {"thread": [{"kind": "reply", "text": "❓ Soll ich X oder Y nehmen?\n\nDetails …"}]}
            check("needs_input: ❓ in Zeile 1 → frage",
                  server.item_needs_input(frage) == "frage")
            spät = {"thread": [{"kind": "reply", "text": "Bericht.\n\nAber ❓ hier unten zählt nicht"}]}
            check("needs_input: ❓ erst im Detailteil zählt NICHT",
                  server.item_needs_input(spät) == "")
            beantwortet = {"thread": [{"kind": "reply", "text": f"Blatt: {blatt}"},
                                      {"kind": "ask", "text": "Hier meine Antworten"}]}
            check("needs_input: die Antwort des Owners löscht den Zustand",
                  server.item_needs_input(beantwortet) == "")
            normal = {"thread": [{"kind": "ask", "text": "mach mal"},
                                 {"kind": "reply", "text": "Erledigt, alles grün."}]}
            check("needs_input: normale Ergebnis-Antwort → nichts",
                  server.item_needs_input(normal) == "")
        finally:
            server.GC_ROOT = orig_root

    pending = {"addr": {"id": "citest", "name": "Cockpit", "col": None},
               "title": "Weekly review", "body": [], "session": "",
               "thread": [{"kind": "ask", "text": "▶ Run Weekly review"}],
               "last_ask": "▶ Run Weekly review"}
    ohne = gc_runner.build_prompt(pending, resume=False)
    check("carryover: ohne pending['carryover'] kein Block", "CARRY-OVER" not in ohne)
    pending["carryover"] = "decision sheet: tmp/decisions/review.html"
    mit = gc_runner.build_prompt(pending, resume=False)
    check("carryover: Block steht im Fresh-Prompt und nennt die Referenz",
          "CARRY-OVER" in mit and "tmp/decisions/review.html" in mit)


def test_action_timeout_per_action() -> None:
    """Die Standard-Notbremse kann für schwere Rituale zu knapp sein. Eine Action darf sie per optionalem
    "timeout" (Sekunden) anheben, ohne dass alle anderen mitwachsen (11.08.,
    Blatt `ai-news-vs-briefing`). Ohne Feld bleibt es beim Default; ein kaputter
    Wert darf den Klick nicht sprengen, sondern fällt auf den Default zurück."""
    import gc_runner
    lang = gc_runner.DEFAULT_TIMEOUT * 2      # bewusst != Default, sonst testet der Fall nichts
    faelle = [("lang-act", lang, lang), ("kurz-act", None, gc_runner.DEFAULT_TIMEOUT),
              ("krumm-act", "bald", gc_runner.DEFAULT_TIMEOUT)]
    actions = []
    for key, to, _erwartet in faelle:
        a = {"key": key, "label": key, "icon": "🧪", "auth": False, "prompt": "Test."}
        if to is not None:
            a["timeout"] = to
        actions.append(a)
    gesehen: list[int] = []
    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    old_actions, orig_launch = server.ACTIONS_FILE, server.launch_gc_run
    with tempfile.TemporaryDirectory() as td:
        try:
            server.ACTIONS_FILE = Path(td) / "actions.json"
            server.ACTIONS_FILE.write_text(json.dumps({"actions": actions}))
            server.launch_gc_run = lambda p, u, c, t, model="": gesehen.append(t) or True
            for key, _to, erwartet in faelle:
                code, _ = server.run_cockpit_action(Path(tmp), key, "http://127.0.0.1:1", "claude")
                check(f"timeout-action: {key} → 202", code == 202)
                check(f"timeout-action: {key} startet mit {erwartet}s",
                      gesehen and gesehen[-1] == erwartet)
        finally:
            server.ACTIONS_FILE, server.launch_gc_run = old_actions, orig_launch
            Path(tmp).unlink(missing_ok=True)


def test_besetzter_port_startet_keine_wachen() -> None:
    """Belegter Port: lesbarer Abbruch — und KEIN Daemon laeuft mehr los.

    Der teure Teil ist nicht die Fehlermeldung, sondern die Reihenfolge: vor dem Fix
    starteten die Hintergrund-Wachen (u. a. `journal_watch`, die per HTTP mit
    127.0.0.1:<port> redet) VOR dem Bind. Ein zweiter Board-Start auf einem belegten
    Port schickte damit Posts und echte claude-Runs gegen das bereits laufende Board
    und starb erst danach am Traceback. Der Bind steht deshalb jetzt vor dem Thread-Start.
    """
    print("\n--- besetzter Port ---")
    import socket

    belegt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    belegt.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    belegt.bind(("127.0.0.1", 0))
    port = belegt.getsockname()[1]
    belegt.listen(1)

    threads_vorher = threading.active_count()
    with tempfile.TemporaryDirectory() as td:
        board = Path(td) / "board.md"
        board.write_text("# Board\n\n# Inbox\n", encoding="utf-8")
        try:
            server.serve(port, board)
            meldung = None
        except SystemExit as e:
            meldung = str(e.code)
        finally:
            belegt.close()

    check("besetzter Port endet in SystemExit, nicht im Traceback", meldung is not None)
    check("Meldung nennt den Port", meldung is not None and str(port) in meldung)
    check("Meldung nennt den Ausweg --port", meldung is not None and "--port" in meldung)
    check("Meldung ist Klartext, kein Errno-Rauschen",
          meldung is not None and "Errno" not in meldung and meldung.startswith("superboard:"))
    # Die Wachen haengen an Daemon-Threads; sie duerfen bei fehlgeschlagenem Bind gar
    # nicht erst laufen.
    time.sleep(0.2)
    check("keine der Wachen startet bei besetztem Port",
          threading.active_count() <= threads_vorher)


def main() -> int:
    # Test-Journale strikt vom echten journal/ trennen: die Journal-Wache eines parallel
    # LAUFENDEN Board-Servers erntet sonst Test-Journale (gc_id dort unbekannt → „schon
    # beantwortet" → Dateien gelöscht, während run_item sein .out.json noch liest) — real
    # als Suite-Flake gesehen 2026-07-16. JOURNAL_DIR ist dafür late-bound (None-Default).
    gc_runner.JOURNAL_DIR = Path(tempfile.mkdtemp(prefix="gc-test-journal-"))
    # Gleiches fürs Git-Anker-File: der echte Anker steuert die Git-Blöcke im
    # laufenden Board — Tests dürfen ihn nicht überschreiben.
    gc_runner.GIT_ANCHOR = gc_runner.JOURNAL_DIR / "git-anchor.json"
    # Gleiches Spiel fürs Usage-Log: run_item loggt via log_usage() ins Modul-Global —
    # ohne Umbiegung landeten pro Suite-Lauf 8 Fake-Zeilen im ECHTEN usage-log.jsonl
    # (real passiert 2026-07-20, 22:57–23:33: 48 Müll-Zeilen zwischen den ersten Echtdaten).
    gc_runner.USAGE_LOG = gc_runner.JOURNAL_DIR / "usage-log-test.jsonl"
    # Und das Neustart-Lock: läuft real gerade ein Board-Neustart-Wächter, würde der Drain
    # jeden Test-Run von launch_gc_run abweisen — die halbe Suite fiele scheinbar um.
    # test_restart_drain setzt sich sein eigenes Lock.
    server.RESTART_LOCK = gc_runner.JOURNAL_DIR / "kein-neustart-lock"
    for fn in (test_real_board_regression, test_meta_lines_roundtrip, test_thread_status,
               test_session_cut,
               test_find_item_by_id, test_ensure_ids, test_guards_block_silent_loss,
               test_gc_pending_endpoint, test_gc_append_hardening, test_csrf_guard_blocks_cross_origin_writes,
               test_body_write_command_reaches_fresh_and_resume_prompts,
               test_lost_guard_haelt_falsche_einrueckung,
               test_gc_append_radar_ist_nativ_ausser_bei_offenem_auftrag,
               test_resume_prompt_traegt_externen_radar_turn_eine_runde_mit,
               test_gc_body_endpoint,
               test_gc_body_chirurgisch_bei_nichtkanonischer_datei, test_gc_append_chirurgisch,
               test_raw_item_blocks, test_new_id_collision_retry,
               test_runner_spawn_envelopes, test_runner_inline_and_sidecar,
               test_gc_run_endpoint, test_gc_run_failure_visible,
               test_sol_final_fixes, test_gc_run_all_and_sidecar_route,
               test_model_choice, test_long_run_policy,
               test_sweep_respects_open_threads, test_sweep_closes_done_threads,
               test_sweep_stamps_missing_done_at,
               test_sweep_retires_chat_cards,
               test_sweep_unknown_thread_kind, test_sweep_heartbeat,
               test_sweep_sidecar_archive, test_sweep_sidecar_collision_warns,
               test_sweep_sidecar_dry_run, test_sweep_sidecar_order_safety,
               test_wait_field, test_wait_decay, test_journal_recovery, test_timeout_default,
               test_version_in_changelog, test_dev_radar_ref_resolution,
               test_dev_radar_review_stale_zeigt_den_letzten_kommentar,
               test_quick_capture_endpoint, test_auto_retrigger, test_interrupt_und_weiter,
               test_gc_last_roundtrip_and_append, test_context_tokens_extraction,
               test_cockpit_endpoint, test_restart_drain,
               test_plain_binary_cannot_be_switched_by_parent_env,
               test_wesen_status, test_attention_hints,
               test_cockpit_section, test_staging_section, test_action_run_endpoint,
               test_testrig_endpoints,
               test_action_run_fresh_session, test_action_timeout_per_action,
               test_hierarchy_datamodel, test_hierarchy_rollup,
               test_hierarchy_spawn_endpoint, test_hierarchy_prompt_context,
               test_hierarchy_sweep_pairing,
               test_chat_send, test_triage,
               test_gc_compact_endpoint,
               test_board_diet_sidecar_module, test_board_diet_append_and_prompt,
               test_board_diet_migration,
               test_ritual_status_daily_weekly, test_ritual_done_endpoint,
               test_ritual_done_proof_none_and_idempotent,
               test_ritual_done_concurrent_no_duplicate,
               test_ritual_snooze_and_gate_override, test_on_field_roundtrip,
               test_meeting_swimlane,
               test_wesen_hungry_precedence, test_wesen_velocity_und_zuckerregel,
               test_wesen_graduiert_und_gedaechtnis,
               test_receipt_fakten_und_retention, test_fehllauf_stempel,
               test_journal_wache_verschont_laufenden_run,
               test_receipt_endpoint,
               test_version_und_changelog_synchron, test_documentation_contract_im_agent_prompt,
               test_stale_client_save_cannot_drop_server_items,
               test_ui_conflict_merge_preserves_external_body,
               test_contract_split_byte_stable, test_contract_protocol_markers_remain_frozen,
               test_stream_parser_beide_formate, test_watch_run_stillstand_vs_arbeit,
               test_kill_trifft_kindergruppe_nicht_uns_selbst,
               test_kill_outcome_und_stop_endpunkt, test_stream_view_und_kill_log,
               test_stream_view_codex_ereignisse, test_stream_view_opencode_ereignisse,
               test_sse_stream_endpoint, test_sse_reconnect_und_ohne_strom,
               test_sse_empty_live_stream_reports_waiting_profile,
               test_bump_entscheidet_minor_vs_patch, test_bump_zaehlt_ersetzte_zeile_einmal,
               test_git_kontext_im_prompt,
               test_netcheck_bewertung, test_index_torso_wird_nie_ausgeliefert,
               test_zwei_prozess_race_haelt_den_flock,
               test_identitaets_guard_reklamiert_verwaiste_id,
               test_live_pfade_umgebogen,
               test_blatt_am_faden_nur_letzter_turn,
               test_needs_input_prädikat_und_carryover,
               test_besetzter_port_startet_keine_wachen,
               test_abhaken_ist_der_fadenschnitt):
        fn()
    print()
    print("ALLE TESTS BESTANDEN" if not FAILS else f"{len(FAILS)} FEHLGESCHLAGEN: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
