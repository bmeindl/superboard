"""Tests für den Codex-Runner (Phase 1 des Plans inbox/analyses/2026-08_codex-runner-PLAN.md).

Warum eine eigene Datei statt Anbau an test_server.py: der Codex-Pfad teilt mit dem
Claude-Pfad nur die Prozesskontrolle, nicht das Datenformat. Die Ereignisströme hier sind
KEINE Erfindung — sie sind aus echten Läufen gegen codex-cli 0.147.0-alpha.6.5 vom
11.08.2026 abgeschrieben (gekürzt auf die Felder, die der Parser anfasst).
"""

from __future__ import annotations

import json
import stat
import tempfile
from pathlib import Path

import gc_runner
import server


# Echter Strom eines Laufs mit einem Shell-Kommando und einer Dateiänderung.
STREAM_OK = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "019ff158-1a77-7e23-a685-366b4e0f391b"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Ich schaue nach."}},
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                      "command": "ls -la\nzweite zeile", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                        "exit_code": 0, "status": "completed"}},
    {"type": "item.started", "item": {"id": "i2", "type": "file_change", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                        "changes": [{"path": "a.txt", "kind": "add"}], "status": "completed"}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Fertig."}},
    {"type": "turn.completed", "usage": {"input_tokens": 26654, "cached_input_tokens": 11008,
                                         "cache_write_input_tokens": 0, "output_tokens": 134,
                                         "reasoning_output_tokens": 0}},
])


def _write_rollout(root: Path, thread_id: str, requests: list[tuple[int, int]],
                   previous_task: bool = False) -> Path:
    """Minimales echtes Rollout-Schema: (input, cached) je Modellanfrage."""
    day = root / "2026" / "08" / "12"
    day.mkdir(parents=True)
    events = []
    if previous_task:
        events += [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "old"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 999999, "cached_input_tokens": 0}}}},
        ]
    events.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "current"}})
    for inp, cached in requests:
        events.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 4_888_728},
            "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached}}}})
    p = day / f"rollout-2026-08-12T17-00-00-{thread_id}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def test_argv_frisch_und_resume() -> None:
    """Die Flags, an denen alles hängt — jeder einzelne ist teuer erkauft."""
    frisch = gc_runner._codex_argv("/bin/codex", "", "codex", Path("/tmp/p"), Path("/tmp/l"))
    assert frisch[:3] == ["/bin/codex", "exec", "--json"]
    # --approve-for-me ist Pflicht (sonst canceln MCP-Calls) und verträgt sich NICHT mit
    # --sandbox: steht das je zusammen im argv, bricht die CLI mit Argument-Konflikt ab.
    assert "--approve-for-me" in frisch and "--sandbox" not in frisch
    assert "--ignore-user-config" in frisch  # ChatGPT-App-MCP-Server draußen halten
    assert frisch[-1] == "-"                 # Prompt über stdin, nie als Argument
    assert "-c" in frisch and 'model_reasoning_effort="medium"' in frisch

    # Das Modell steht seit 2026-08-12 im Profil statt im CLI-Default (s. RUN_PROFILES).
    assert "-m" in frisch and "gpt-5.6-sol" in frisch

    res = gc_runner._codex_argv("/bin/codex", "abc-123", "codex-xhigh", Path("/tmp/p"), Path("/tmp/l"))
    assert res[-3:] == ["resume", "abc-123", "-"]  # Session-ID direkt hinter dem Unterbefehl
    assert 'model_reasoning_effort="xhigh"' in res
    # "ultra" ist eine echte Effort-Stufe von gpt-5.6-sol, kein Tippfehler — gegen
    # codex-cli 0.147.0 verifiziert (Rollout-Log schreibt "effort":"ultra").
    ultra = gc_runner._codex_argv("/bin/codex", "", "codex-ultra", Path("/tmp/p"), Path("/tmp/l"))
    assert 'model_reasoning_effort="ultra"' in ultra


def test_profile_weiche() -> None:
    assert gc_runner.runner_of("codex") == "codex"
    assert gc_runner.runner_of("codex-high") == "codex"
    assert gc_runner.runner_of("opus") == "claude"
    assert gc_runner.runner_of("quatsch") == "claude"  # Unbekanntes bleibt beim Default
    assert "codex" in gc_runner.RUN_PROFILES  # sonst lehnt der Server das Profil mit 400 ab


def test_envelope_und_parse() -> None:
    env, tid, last = gc_runner._codex_envelope(STREAM_OK)
    assert tid == "019ff158-1a77-7e23-a685-366b4e0f391b"
    assert last == "Fertig."  # letzte agent_message, nicht die erste
    assert env["usage"]["input_tokens"] == 26654

    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "last.txt"
        lp.write_text("Antwort aus der -o-Datei\n")
        out = gc_runner._parse_codex_stdout(STREAM_OK, "", 0, lp, "codex")
        # Die -o-Datei schlägt die letzte agent_message: die kann auch eine Zwischenansage sein.
        assert out["ok"] and out["reply"] == "Antwort aus der -o-Datei"
        assert out["session_id"] == "019ff158-1a77-7e23-a685-366b4e0f391b"
        assert out["usage_summary"]["cost_usd"] is None  # Codex liefert keine USD — nichts erfinden
        assert out["usage_summary"]["cache_read"] == 11008
        # Seit dem Modell-Pin steht der echte Slug im Log statt "codex:default" — damit
        # eine Auswertung später sieht, WELCHES Codex-Modell den Run gefahren hat.
        assert out["usage_summary"]["models"] == ["codex:gpt-5.6-sol"]


def test_rollout_liefert_ehrlichen_kontext_und_cross_run_cache() -> None:
    """Regression für „Kontext ~4198k": Turn-Summe bleibt Verbrauch, nie Kontext."""
    thread_id = "019ff158-1a77-7e23-a685-366b4e0f391b"
    gross = STREAM_OK.replace('"input_tokens": 26654', '"input_tokens": 4888728') \
                     .replace('"cached_input_tokens": 11008', '"cached_input_tokens": 4398592')
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_rollout(root, thread_id, [(31_174, 26_112), (173_075, 172_800)], previous_task=True)
        out = gc_runner._parse_codex_stdout(gross, "", 0, None, "codex", root)

    assert out["ok"]
    assert out["context_tokens"] == 173_075
    usage = out["usage_summary"]
    assert usage["input_tokens"] == 4_888_728          # Run-Verbrauch bleibt sichtbar
    assert usage["last_request_input_tokens"] == 173_075
    assert usage["cross_run_input_tokens"] == 31_174
    assert usage["cross_run_cache_read"] == 26_112
    assert usage["cross_run_cache_hit_pct"] == 84


def test_fehlender_rollout_erfindet_keinen_kontext() -> None:
    """CLI-internes Format darf driften, ohne zur kumulierten Falschaussage zurückzufallen."""
    with tempfile.TemporaryDirectory() as td:
        out = gc_runner._parse_codex_stdout(STREAM_OK, "", 0, None, "codex", Path(td))
    assert out["ok"] and out["context_tokens"] == 0
    assert "last_request_input_tokens" not in out["usage_summary"]


def test_cache_beobachtung_kommt_nur_aus_codex_resume_log(monkeypatch, tmp_path) -> None:
    log = tmp_path / "usage.jsonl"
    rows = [
        {"gc_id": "aaaaaaaaaaaa", "model": "codex", "resumed": False,
         "cross_run_input_tokens": 15_575, "cross_run_cache_read": 11_008},
        {"gc_id": "aaaaaaaaaaaa", "model": "codex-xhigh", "resumed": True,
         "ts": "2026-08-12 17:00:00", "cross_run_input_tokens": 31_174,
         "cross_run_cache_read": 26_112, "cross_run_cache_hit_pct": 84,
         "context_source": "codex-rollout"},
        {"gc_id": "bbbbbbbbbbbb", "model": "opus", "resumed": True,
         "cross_run_input_tokens": 99, "cross_run_cache_read": 99},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(gc_runner, "USAGE_LOG", log)
    board = {"themes": [{"name": "T", "cols": {"Jetzt": [
        {"id": "aaaaaaaaaaaa"}, {"id": "bbbbbbbbbbbb"}], "Bald": [], "Geparkt": []}}],
        "persons": [], "staging": [], "cockpit": []}
    server.annotate_cross_run_cache(board)
    codex, claude = board["themes"][0]["cols"]["Jetzt"]
    assert codex["cache_observation"]["cross_run_cache_hit_pct"] == 84
    # Claude-Zeile traegt zwar Codex-Felder (kann real nie passieren), aber keine
    # Turn-1-Messung — daraus darf keine Bilanz entstehen.
    assert "cache_observation" not in claude


def test_claude_cross_run_kommt_aus_turn_eins(monkeypatch, tmp_path) -> None:
    """Fuer Claude rechnen wir die Cross-Run-Bilanz selbst: read / (read+write+input) des
    ERSTEN Modellaufrufs. Das run-weite cache_hit_pct waere Within-Run und immer ~96 %."""
    log = tmp_path / "usage.jsonl"
    rows = [
        # Fresh-Lauf: kein Cross-Run-Statement moeglich, auch mit Turn-1-Zahlen.
        {"gc_id": "cccccccccccc", "model": "opus", "resumed": False,
         "t1_read": 17_617, "t1_write": 300, "t1_input": 12},
        {"gc_id": "dddddddddddd", "model": "opus", "resumed": True, "ts": "2026-08-13 09:05:23",
         "t1_read": 17_617, "t1_write": 37_390, "t1_input": 0, "ttl": "5m"},
        # Altbestand ohne t1_input: faellt auf read+write zurueck, statt zu verschwinden.
        {"gc_id": "eeeeeeeeeeee", "model": "sonnet", "resumed": True, "ts": "2026-08-12 18:42:58",
         "t1_read": 0, "t1_write": 128_238},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(gc_runner, "USAGE_LOG", log)
    board = {"themes": [{"name": "T", "cols": {"Jetzt": [
        {"id": "cccccccccccc"}, {"id": "dddddddddddd"}, {"id": "eeeeeeeeeeee"}],
        "Bald": [], "Geparkt": []}}], "persons": [], "staging": [], "cockpit": []}
    server.annotate_cross_run_cache(board)
    fresh, resume, alt = board["themes"][0]["cols"]["Jetzt"]
    assert "cache_observation" not in fresh
    assert resume["cache_observation"] == {
        "ts": "2026-08-13 09:05:23", "cross_run_input_tokens": 55_007,
        "cross_run_cache_read": 17_617, "cross_run_cache_hit_pct": 32,
        "context_source": "claude-turn1", "ttl": "5m"}
    assert alt["cache_observation"]["cross_run_cache_hit_pct"] == 0


def test_parse_vertraegt_muell_und_fehler() -> None:
    """Real beobachtet: CLI-Fehler kommen als Klartext, nicht als JSON; stderr trägt bei
    jedem Lauf OAuth-Warnungen. Beides darf keinen Run töten."""
    dreck = "Reading additional input from stdin...\n" + STREAM_OK + '\n{"halb geschrieben'
    out = gc_runner._parse_codex_stdout(dreck, "warnung", 0, None, "codex")
    assert out["ok"] and out["reply"] == "Fertig."

    fehl = json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n" + \
        json.dumps({"type": "turn.failed", "error": {"message": "model overloaded"}})
    bad = gc_runner._parse_codex_stdout(fehl, "", 1, None, "codex")
    assert not bad["ok"] and "model overloaded" in bad["raw_error"]
    assert bad["session_id"] == "t1"  # Session bleibt erhalten → Faden ist fortsetzbar

    # Abbruch per SIGTERM: Strom endet ohne Abschluss-Event, Exit 143, keine Schlussnachricht.
    ab = gc_runner._parse_codex_stdout(json.dumps({"type": "thread.started", "thread_id": "t2"}),
                                       "", 143, None, "codex")
    assert not ab["ok"] and ab["session_id"] == "t2"


def test_stream_tail_zaehlt_werkzeuge() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "out.jsonl"
        p.write_text(STREAM_OK.split('{"type": "item.completed", "item": {"id": "i1"')[0])
        tail = gc_runner.CodexStreamTail(p)
        st = tail.poll()
        assert st["session_id"] == "019ff158-1a77-7e23-a685-366b4e0f391b"
        assert st["steps"] == 1                    # agent_message zählt NICHT als Werkzeug
        assert st["last_tool"] == "ls -la"         # nur die erste Zeile des Befehls
        assert st["busy"] == 1                     # Werkzeug läuft noch

        p.write_text(STREAM_OK)                    # Rest nachschieben
        st = tail.poll()
        assert st["steps"] == 2 and st["busy"] == 0 and st["busy_tool"] == ""


def test_stream_tail_mcp_werkzeugname() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "out.jsonl"
        p.write_text(json.dumps({"type": "item.started", "item": {
            "id": "m1", "type": "mcp_tool_call", "server": "jira", "tool": "jira_get_issue"}}) + "\n")
        st = gc_runner.CodexStreamTail(p).poll()
        assert st["last_tool"] == "jira/jira_get_issue"


def test_ui_profile_liste_deckt_sich_mit_dem_runner() -> None:
    """Das Dropdown in index.html führt die Profilnamen ein zweites Mal (nur die
    Beschriftungen leben dort). Läuft die Liste auseinander, lehnt der Server das
    angebotene Profil mit 400 ab — ein Fehler, den man erst beim Klicken merkt."""
    import re
    src = (Path(__file__).parent / "index.html").read_text()
    block = re.search(r"const RUN_PROFILES = \[(.*?)\];", src, re.S)
    assert block, "RUN_PROFILES nicht mehr in index.html gefunden"
    ui = set(re.findall(r'\["([a-z0-9-]+)",', block.group(1)))
    assert ui <= set(gc_runner.RUN_PROFILES), f"UI bietet unbekannte Profile: {ui - set(gc_runner.RUN_PROFILES)}"
    assert {"codex", "codex-xhigh", "codex-ultra"} <= ui  # die Codex-Profile sind wählbar
    # Umgekehrt NICHT: "codex-high" ist der Alt-Name (Dropdown kennt ihn nicht mehr,
    # der Runner schon — sonst fiele ein Item mit altem localStorage-Wert auf Claude).
    assert "codex-high" not in ui and "codex-high" in gc_runner.RUN_PROFILES
    assert 'itemRunner(it) !== "codex"' in src


def test_codex_auswahl_bleibt_pro_item_und_wird_nicht_globaler_default() -> None:
    """Der Produktvertrag lautet: Codex explizit pro Task, Default bleibt Claude."""
    src = (Path(__file__).parent / "index.html").read_text()
    assert 'if (isPerItemProfile(v)) { localStorage.setItem("board.agentModel", "")' in src
    assert 'if (!isPerItemProfile(v)) localStorage.setItem("board.agentModel", v);' in src
    assert 'if (j.id) setAgentModel(j.id, cm.value)' in src  # Quick-Capture merkt es am neuen Item
    assert '["", "Claude default · Recommended"]' in src
    assert 'return v ?? "";' in src


def test_session_runner_marker() -> None:
    """Der Marker hängt am Session-Label. Bestehende Claude-Zeilen bleiben unverändert —
    ohne Marker ist Claude gemeint, nicht „unbekannt"."""
    assert gc_runner.session_runner("abc · board-item · codex") == "codex"
    assert gc_runner.session_runner("abc · board-item") == "claude"
    assert gc_runner.session_runner("") == "claude"
    # Der Handle selbst bleibt vom Marker unberührt (sonst bräche --resume).
    assert gc_runner.session_uuid("fa4e5e55-0000-4000-8000-00000000e2e1 · board-x · codex") == \
        "fa4e5e55-0000-4000-8000-00000000e2e1"


def test_outcome_schreibt_marker_nur_fuer_codex() -> None:
    with tempfile.TemporaryDirectory() as td:
        sid = "fa4e5e55-0000-4000-8000-00000000e2e1"
        basis = {"ok": True, "reply": "hi", "session_id": sid, "denials": [],
                 "context_tokens": 100, "usage_summary": {}}
        _, s_codex, _ = gc_runner._outcome({**basis, "runner": "codex"}, "id", "Titel", Path(td))
        _, s_claude, _ = gc_runner._outcome({**basis, "runner": "claude"}, "id", "Titel", Path(td))
        assert s_codex.endswith(" · codex") and gc_runner.session_runner(s_codex) == "codex"
        assert not s_claude.endswith(" · codex")


def test_parse_by_runner_waehlt_den_parser() -> None:
    """Journal-Recovery nach Serverneustart: der Strom eines Codex-Runs darf nicht durch
    den Claude-Parser laufen — der fände dort kein Ergebnis und postete ein ❌."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "run.out"
        out.write_text(STREAM_OK)
        (Path(td) / "run.last").write_text("aus der -o-Datei")
        r = gc_runner.parse_by_runner("codex", STREAM_OK, "", None, out)
        assert r["ok"] and r["reply"] == "aus der -o-Datei" and r["runner"] == "codex"
        # Dasselbe Profil-Feld, aber ein Claude-Lauf → Claude-Parser.
        claude_env = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "result": "claude-antwort", "session_id": "s1"})
        c = gc_runner.parse_by_runner("opus", claude_env, "", 0, None)
        assert c["ok"] and c["reply"] == "claude-antwort" and c["runner"] == "claude"


def _fake_codex(dirpath: Path, body: str) -> str:
    """Fake-codex-Binary nach dem Muster von _fake_claude in test_server.py — testet
    Spawn → stdin-Prompt → Parse, ohne Abo-Kontingent zu verbrennen."""
    p = dirpath / "codex"
    p.write_text("#!/usr/bin/env python3\nimport json, os, sys\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_spawn_codex_end_to_end(monkeypatch) -> None:
    """Der Prompt MUSS über stdin ankommen (als Argument bricht die echte CLI bei
    Kernel-Länge ab) und die -o-Datei muss geschrieben werden können."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        body = (
            "argv = sys.argv[1:]\n"
            "prompt = sys.stdin.read()\n"
            "out = argv[argv.index('-o') + 1]\n"
            "open(out, 'w').write(json.dumps({'prompt': prompt.strip(), "
            "'claude_config': os.environ.get('CLAUDE_CONFIG_DIR'), "
            "'anthropic_token': os.environ.get('ANTHROPIC_AUTH_TOKEN'), "
            "'board_url': os.environ.get('GC_BOARD_URL')}))\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'c0ffee00-0000-4000-8000-000000000001'}))\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n"
        )
        monkeypatch.setattr(gc_runner, "BASE_ENV", {
            **gc_runner.BASE_ENV,
            "CLAUDE_CONFIG_DIR": "/tmp/alternate-account",
            "ANTHROPIC_AUTH_TOKEN": "do-not-pass",
        })
        board_url = "http://127.0.0.1:47901"
        out = gc_runner.spawn_codex(
            "hallo welt", "", _fake_codex(d, body), 20, "codex",
            extra_env={"GC_BOARD_URL": board_url},
        )
        assert out["ok"], out
        reply = json.loads(out["reply"])
        assert reply == {"prompt": "hallo welt", "claude_config": None,
                         "anthropic_token": None, "board_url": board_url}
        assert out["session_id"] == "c0ffee00-0000-4000-8000-000000000001"

        weg = gc_runner.spawn_codex("p", "", str(d / "gibts-nicht"), 5, "codex")
        assert not weg["ok"] and "not found" in weg["raw_error"]


def test_spawn_agent_waehlt_den_runner(monkeypatch) -> None:
    """Die Weiche darf nur am Profil hängen — nicht an einem zweiten Auswahlfeld."""
    gerufen = []
    monkeypatch.setattr(gc_runner, "spawn_claude", lambda *a, **k: gerufen.append("claude") or {})
    monkeypatch.setattr(gc_runner, "spawn_codex", lambda *a, **k: gerufen.append("codex") or {})
    gc_runner.spawn_agent("p", "", "claude", 10, "opus")
    gc_runner.spawn_agent("p", "", "claude", 10, "codex")
    gc_runner.spawn_agent("p", "", "claude", 10, "")
    assert gerufen == ["claude", "codex", "claude"]


# --- Phase 3+4: Kernel-Zuführung und Skill-Verlinkung ----------------------------------

def _pending() -> dict:
    return {"addr": {"id": "aaaabbbbcccc", "name": "Dev", "col": "Jetzt"}, "title": "Testitem",
            "body": ["Notiz"], "thread": [{"kind": "ask", "text": "mach was"}],
            "last_ask": "mach was", "session": ""}


def test_kernel_nur_fuer_codex_und_nur_frisch(monkeypatch, tmp_path) -> None:
    """Der Kernel ist der teuerste Block im Prompt — er darf exakt einmal auftauchen:
    beim ersten Turn eines Codex-Fadens. Claude lädt ihn selbst, Resume kennt ihn schon."""
    (tmp_path / "CLAUDE.md").write_text("# Test kernel\n")
    monkeypatch.setattr(gc_runner, "GC_ROOT", tmp_path)
    marke = '<kernel file="CLAUDE.md">'
    frisch_codex = gc_runner.build_prompt(_pending(), resume=False, runner="codex")
    assert marke in frisch_codex
    assert frisch_codex.index(marke) < frisch_codex.index("Board item")  # steht ganz vorn
    assert marke not in gc_runner.build_prompt(_pending(), resume=False, runner="claude")
    assert marke not in gc_runner.build_prompt(_pending(), resume=True, runner="codex")
    assert marke not in gc_runner.build_prompt(_pending(), resume=False)  # Default = claude


def test_kontrakt_nennt_resume_befehl_des_runners() -> None:
    """Phase 7: Der Kontrakt weist den Agenten an, im Auth-Handoff einen Resume-Befehl
    zu nennen — für Codex muss das `codex resume` (voller Binary-Pfad, nicht im PATH)
    sein, sonst schreibt der Agent einen Befehl, der seine Session nie erreicht."""
    codex = gc_runner._contract_for("codex")
    assert "claude --resume" not in codex
    assert f"`{gc_runner.CODEX_CMD} resume <SESSION>`" in codex
    assert "thread ID" in codex  # kopiert wird die Thread-ID, keine Session-UUID
    default = gc_runner._contract_for("claude")
    assert f"`{gc_runner.PRIVATE_CMD} --resume <SESSION>`" in default
    # Ende-zu-Ende über build_prompt: frischer Prompt trägt den Befehl des Runners.
    assert f"{gc_runner.CODEX_CMD} resume <SESSION>" in gc_runner.build_prompt(
        _pending(), resume=False, runner="codex")
    assert f"{gc_runner.PRIVATE_CMD} --resume <SESSION>" in gc_runner.build_prompt(
        _pending(), resume=False, runner="claude")


def test_kernel_block_stirbt_nicht_an_fehlender_datei(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gc_runner, "GC_ROOT", tmp_path)
    assert gc_runner._kernel_block() == ""


def test_ensure_codex_skills(tmp_path) -> None:
    """Idempotent, legt beide Verzeichnis-Links an — und fasst nichts an, was schon da ist."""
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    assert gc_runner.ensure_codex_skills(tmp_path) == {
        ".codex/skills": "angelegt", ".agents/skills": "angelegt"}
    for rel in (".codex/skills", ".agents/skills"):
        link = tmp_path / rel
        assert link.is_symlink() and link.resolve() == (tmp_path / ".claude" / "skills").resolve()
    assert set(gc_runner.ensure_codex_skills(tmp_path).values()) == {"ok"}  # zweiter Lauf ändert nichts

    anderer = tmp_path / "anders"
    anderer.mkdir()
    link = tmp_path / ".codex" / "skills"
    link.unlink()
    link.symlink_to(anderer)
    assert gc_runner.ensure_codex_skills(tmp_path)[".codex/skills"] == "fremder-link"
    link.unlink()
    link.mkdir()
    assert gc_runner.ensure_codex_skills(tmp_path)[".codex/skills"] == "belegt"


def test_ensure_codex_skills_ohne_quelle(tmp_path) -> None:
    assert set(gc_runner.ensure_codex_skills(tmp_path).values()) == {"kein-quellverzeichnis"}
    assert not (tmp_path / ".codex").exists()


# ---------------------------------------------------------------- Phase 5: MCP / CODEX_HOME

MCP_JSON = {"mcpServers": {
    "tracker-example": {
        "command": "docker", "args": ["run", "-i", "--rm", "img"],
        "env": {"TRACKER_URL": "https://tracker.example", "TRACKER_TOKEN": "secret123"}},
    "wiki-example": {
        "command": "docker", "args": [],
        "env": {"WIKI_TOKEN": "secret456"}},
    "remote-example": {"url": "https://mcp.example/sse"},
    "ignored-stdio": {"command": "uvx", "args": ["x"], "env": {"IGNORED_KEY": "nope"}},
}}


def test_generate_codex_config_nur_namen_keine_werte() -> None:
    """Der Kern von Phase 5: Secrets stehen NIE in der generierten Datei — nur
    env_vars-NAMEN (verifiziert am Binary: Codex zieht die Werte aus dem Spawn-Env)."""
    toml_text, secret_env = gc_runner.generate_codex_config(MCP_JSON, Path("/repo"))
    assert "secret123" not in toml_text and "secret456" not in toml_text
    assert secret_env == {}
    # Standalone v0 forwards no private-instance MCP configuration.
    assert "tracker-example" not in toml_text and "wiki-example" not in toml_text
    assert "ignored-stdio" not in toml_text and "remote-example" not in toml_text
    # Shell-Kommandos des Agents bekommen die Tokens nicht (inherit=core)
    assert 'inherit = "core"' in toml_text
    assert '[projects."/repo"]' in toml_text


def test_generate_codex_config_env_kollision() -> None:
    mcp = {"mcpServers": {
        "tracker-example": {"command": "a", "env": {"TOKEN": "one"}},
        "wiki-example": {"command": "b", "env": {"TOKEN": "two"}},
    }}
    old = gc_runner.CODEX_MCP_SERVERS
    gc_runner.CODEX_MCP_SERVERS = ("tracker-example", "wiki-example")
    try:
        try:
            gc_runner.generate_codex_config(mcp, Path("/repo"))
            raise AssertionError("Kollision muss ValueError werfen")
        except ValueError:
            pass
    finally:
        gc_runner.CODEX_MCP_SERVERS = old


def test_link_shared_codex_state(tmp_path) -> None:
    """Auth und Sessions werden mit ~/.codex GETEILT (spart den zweiten Login, haelt
    Faeden resumebar); nur die config.toml ist isoliert. Echte Dateien bleiben liegen —
    ein Zuruecklinken wuerde frisch refreshte Tokens gegen aeltere tauschen."""
    user = tmp_path / "dot-codex"
    (user / "sessions").mkdir(parents=True)
    (user / "auth.json").write_text('{"tokens": {}}')
    home = tmp_path / "board-home"
    home.mkdir()

    r = gc_runner._link_shared_codex_state(home, user)
    assert r == {"auth.json": "angelegt", "sessions": "angelegt"}
    assert (home / "auth.json").is_symlink() and (home / "auth.json").is_file()
    assert gc_runner.codex_home_ready(home)
    assert gc_runner._link_shared_codex_state(home, user)["auth.json"] == "ok"  # idempotent

    # Codex hat den Link beim Token-Refresh durch eine echte Datei ersetzt -> nicht anfassen
    (home / "auth.json").unlink()
    (home / "auth.json").write_text('{"tokens": {"neu": 1}}')
    assert gc_runner._link_shared_codex_state(home, user)["auth.json"] == "belegt"
    assert (home / "auth.json").read_text() == '{"tokens": {"neu": 1}}'

    # kein ~/.codex vorhanden -> kein Link, nicht bereit, aber auch kein Fehler
    leer = tmp_path / "leer-home"
    leer.mkdir()
    assert gc_runner._link_shared_codex_state(leer, tmp_path / "gibts-nicht") == {
        "auth.json": "kein-ziel", "sessions": "kein-ziel"}
    assert not gc_runner.codex_home_ready(leer)


def test_codex_home_ready_toter_link(tmp_path) -> None:
    """Ein Link ins Leere ist keine Anmeldung — sonst liefe der Lauf im MCP-Modus los
    und Codex wuerde erst beim Auth-Check scheitern."""
    home = tmp_path / "h"
    home.mkdir()
    (home / "auth.json").symlink_to(tmp_path / "weg.json")
    assert not gc_runner.codex_home_ready(home)


def test_prepare_codex_home_und_ready(tmp_path) -> None:
    """Happy Path: config.toml landet im Board-Home; ready erst mit auth.json.
    Kaputte .mcp.json -> (None, {}) statt Exception (MCP ist Komfort, kein Run-Killer)."""
    (tmp_path / ".mcp.json").write_text(json.dumps(MCP_JSON))
    home, secrets = gc_runner.prepare_codex_home(tmp_path)
    assert home == tmp_path / ".superboard" / "codex-home"
    cfg = (home / "config.toml").read_text()
    assert "geheim123" not in cfg and "JIRA_API_TOKEN" not in cfg
    assert secrets == {}
    # ohne echtes ~/.codex bleibt das Home unangemeldet; eine eigene auth.json macht bereit
    if not gc_runner.codex_home_ready(home):
        (home / "auth.json").write_text("{}")
    assert gc_runner.codex_home_ready(home)
    # idempotent: zweiter Lauf schreibt nicht neu (mtime bleibt)
    m1 = (home / "config.toml").stat().st_mtime_ns
    gc_runner.prepare_codex_home(tmp_path)
    assert (home / "config.toml").stat().st_mtime_ns == m1

    (tmp_path / ".mcp.json").write_text("{kaputt")
    assert gc_runner.prepare_codex_home(tmp_path) == (None, {})
    assert not gc_runner.codex_home_ready(None)


def test_argv_own_home_laesst_config_zu() -> None:
    """Mit Board-Home MUSS --ignore-user-config weg (es wuerde genau unsere generierte
    config.toml verwerfen); ohne bleibt es drin (ChatGPT-App-Server aussperren)."""
    ohne = gc_runner._codex_argv("/bin/codex", "", "codex", Path("/p"), Path("/l"))
    mit = gc_runner._codex_argv("/bin/codex", "", "codex", Path("/p"), Path("/l"), own_home=True)
    assert "--ignore-user-config" in ohne
    assert "--ignore-user-config" not in mit
