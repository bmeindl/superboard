"""Terminal-Sidecar: Eingabe-Härtung, geteilter Betrachter (eine Sitzung pro Item), Lebenszyklus.

Die teuren Teile (tmux startet wirklich, ttyd bindet wirklich) laufen als echte
Integration, wenn die Binaries da sind — mit `cat` statt `claude`, damit kein
Agent gestartet wird. Alles andere läuft gegen Doubles, weil sonst jeder Lauf
Prozesse hinterlässt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import terminal


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(terminal, "INPUT_DIR", tmp_path / "input")


# ------------------------------------------------------------ Eingabe-Härtung


def test_item_id_wird_streng_geprueft():
    assert terminal.tmux_name("596cd041c2e1") == "gcterm-596cd041c2e1"
    # Punkt und Doppelpunkt sind in tmux-Namen Sonderzeichen, Leerzeichen und
    # Semikolon wären der Weg in eine fremde Kommandozeile.
    for boese in ["", "a b", "a;rm -rf /", "a.b", "a:b", "x" * 41]:
        with pytest.raises(terminal.TerminalError):
            terminal.tmux_name(boese)


def test_resume_cmd_kennt_alle_runner_und_lehnt_muell_ab():
    uuid = "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74"
    private = terminal.resume_cmd("claude", uuid)
    assert private[0] == terminal._CLAUDE
    assert private[1:] == ["--resume", uuid]
    # Codex liegt nicht im PATH — der volle Pfad ist Teil des Vertrags.
    codex = terminal.resume_cmd("codex", uuid)
    assert codex[0].endswith("/codex") and codex[1:] == ["resume", uuid]
    assert terminal.RUNNER_ENV["codex"]["CODEX_HOME"].endswith(".superboard/codex-home")
    with pytest.raises(terminal.TerminalError):
        terminal.resume_cmd("bash", uuid)
    with pytest.raises(terminal.TerminalError):
        terminal.resume_cmd("claude", "$(whoami)")


def test_jeder_runner_des_boards_hat_einen_resume_befehl():
    """Der Guard gegen die stille Lücke: kommt im Runner ein fünfter Runner dazu,
    darf sein Terminal-Knopf nicht erst beim Klicken auffallen."""
    import gc_runner

    profile = gc_runner.CODEX_PROFILES | {"opus", "sonnet", "was-auch-immer"}
    assert {gc_runner.runner_of(p) for p in profile} <= set(terminal.RESUME)


def test_private_resume_scrubs_tmux_server_identity(monkeypatch):
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        code = 1 if "has-session" in cmd else 0
        return subprocess.CompletedProcess(cmd, code, "", "")

    monkeypatch.setattr(terminal, "_run", fake_run)
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/fake")

    terminal.ensure_tmux("privateprobe", "claude",
                         "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")

    launch = next(cmd for cmd in calls if "new-session" in cmd)
    shell = launch[-1]
    assert ("unset CLAUDE_CONFIG_DIR ANTHROPIC_BASE_URL "
            "ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY") in shell
    assert "export CLAUDE_CONFIG_DIR" not in shell
    assert "claude --resume" in shell


# ------------------------------------------------------------ Ein Betrachter


class _FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def _fake_welt(monkeypatch, lebende: set[int], gestartet: list):
    """tmux/ttyd durch Doubles ersetzen: nichts startet, alles ist beobachtbar."""
    monkeypatch.setattr(terminal.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(terminal, "has_tmux", lambda item: True)
    # tmux-Nebenbefehle (set-option beim Nachziehen einer Sitzung) laufen ins Leere.
    monkeypatch.setattr(terminal, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(terminal, "_alive", lambda pid: pid in lebende)
    # Der Port ist genau dann frei, wenn kein Betrachter mehr lebt.
    monkeypatch.setattr(terminal, "_port_free", lambda port: not lebende)
    getoetet = []
    monkeypatch.setattr(terminal.os, "kill", lambda pid, sig: (
        getoetet.append(pid), lebende.discard(pid)))

    def _popen(cmd, **kw):
        gestartet.append(cmd)
        proc = _FakeProc(pid=9000 + len(gestartet))
        lebende.add(proc.pid)
        return proc

    monkeypatch.setattr(terminal.subprocess, "Popen", _popen)
    return getoetet


def test_zweites_item_teilt_den_einen_betrachter(monkeypatch):
    """Seit 07.09.2026 (the owner: „es scheint nur eine aktive Terminal-Session zu geben"):
    EIN ttyd mit `-a`, jedes Item bekommt seine eigene URL — nichts wird umgehängt."""
    lebende, gestartet = set(), []
    getoetet = _fake_welt(monkeypatch, lebende, gestartet)
    monkeypatch.setattr(terminal, "held_items", lambda: ["itemeins", "itemzwei"])
    uuid = "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74"

    erst = terminal.open_terminal("itemeins", "claude", uuid)
    zweit = terminal.open_terminal("itemzwei", "codex", uuid)

    assert erst["port"] == zweit["port"] == terminal.PORT
    assert getoetet == [], "der Betrachter ist geteilt — niemand muss weichen"
    assert len(gestartet) == 1 and erst["ttyd_pid"] == zweit["ttyd_pid"]
    assert erst["url"].endswith("?arg=gcterm-itemeins")
    assert zweit["url"].endswith("?arg=gcterm-itemzwei")
    assert zweit["held"] == ["itemeins", "itemzwei"]
    cmd = gestartet[0]
    assert "-a" in cmd, "die Sitzung kommt aus der URL"
    assert cmd[-3:] == ["sh", "-c", terminal.ATTACH_SCRIPT] or cmd[-4:-1] == ["sh", "-c", terminal.ATTACH_SCRIPT]
    assert 'attach -t "$1";;' in terminal.ATTACH_SCRIPT, "schreibbar: kein -r beim Attach"
    assert "-W" in cmd, "seit 06.09.2026 tippt man ins Terminal (the owner)"
    assert "-O" in cmd, "WebSocket nur vom eigenen Origin"
    assert "127.0.0.1" in cmd, "kein offener Port nach außen"
    assert "item" not in json.loads(terminal.STATE_PATH.read_text()), "der Betrachter kennt kein Item mehr"


def test_attach_skript_laesst_nur_gcterm_sitzungen_zu(tmp_path):
    """Das URL-Argument kommt aus dem Browser: nur `gcterm-*`, alles andere wird benannt
    statt ausgeführt. Läuft gegen ein tmux-Double, damit nichts echtes attached wird."""
    fake = tmp_path / "tmux"
    fake.write_text("#!/bin/sh\necho TMUX \"$@\"\n")
    fake.chmod(0o755)
    script = terminal.ATTACH_SCRIPT.replace(terminal.TMUX, str(fake), 1)

    def lauf(arg):
        return subprocess.run(["sh", "-c", script, "gc-attach", arg],
                              capture_output=True, text=True, input="\n", timeout=5).stdout
    assert lauf("gcterm-itemeins").strip() == "TMUX attach -t gcterm-itemeins"
    assert "no session named main" in lauf("main")
    assert "TMUX" not in lauf("-t")


def test_dasselbe_item_startet_nichts_neu(monkeypatch):
    lebende, gestartet = set(), []
    _fake_welt(monkeypatch, lebende, gestartet)
    monkeypatch.setattr(terminal, "held_items", lambda: ["itemeins"])
    uuid = "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74"

    terminal.open_terminal("itemeins", "claude", uuid)
    wieder = terminal.open_terminal("itemeins", "claude", uuid)

    assert wieder["reused"] is True
    assert len(gestartet) == 1


def test_status_raeumt_verwaisten_zustand_auf(monkeypatch):
    terminal.STATE_PATH.write_text(json.dumps({"ttyd_pid": 1, "port": 1}))
    monkeypatch.setattr(terminal, "_alive", lambda pid: False)

    assert terminal.status() == {"open": False}
    assert terminal.STATE_PATH.read_text() == "{}"


def test_close_beendet_den_betrachter_und_laesst_tmux_stehen(monkeypatch):
    lebende, gestartet = {77}, []
    _fake_welt(monkeypatch, lebende, gestartet)
    terminal.STATE_PATH.write_text(json.dumps({"ttyd_pid": 77, "port": terminal.PORT}))
    kill_aufrufe = []
    monkeypatch.setattr(terminal, "kill_tmux", lambda item: kill_aufrufe.append(item))

    assert terminal.close() == {"open": False}
    assert kill_aufrufe == [], "der Zustand des Items überlebt das Schließen"


def test_release_beendet_tmux_und_den_betrachter_erst_mit_der_letzten_sitzung(monkeypatch):
    """Besitzregel: „Return to board" gibt die Session zurück — tmux stirbt; der geteilte
    Betrachter erst, wenn kein Item mehr eine Sitzung hat."""
    lebende, gestartet = set(), []
    getoetet = _fake_welt(monkeypatch, lebende, gestartet)
    gehalten = ["itemeins", "itemzwei"]
    monkeypatch.setattr(terminal, "held_items", lambda: list(gehalten))
    gekillt = []
    monkeypatch.setattr(terminal, "kill_tmux",
                        lambda item: (gekillt.append(item), gehalten.remove(item)) and True)
    uuid = "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74"
    erst = terminal.open_terminal("itemeins", "claude", uuid)

    out = terminal.release("itemzwei")                  # anderes Item: Betrachter bleibt
    assert out["released"] and gekillt == ["itemzwei"] and getoetet == []
    assert out["held"] == ["itemeins"] and terminal.status()["open"]

    out = terminal.release("itemeins")
    assert gekillt == ["itemzwei", "itemeins"] and getoetet == [erst["ttyd_pid"]]
    assert out["held"] == [] and terminal.status() == {"open": False}


def test_holds_session_ist_fail_open(monkeypatch):
    monkeypatch.setattr(terminal, "has_tmux", lambda item: True)
    assert terminal.holds_session("itemeins")
    assert not terminal.holds_session("../böse"), "unbrauchbare Id hält nichts"
    def _boom(item):
        raise OSError("kein tmux")
    monkeypatch.setattr(terminal, "has_tmux", _boom)
    assert not terminal.holds_session("itemeins"), "kaputtes Terminal blockiert keinen Run"


def test_verwaister_ttyd_wird_eingesammelt_fremdes_nicht(monkeypatch):
    """Der Betrachter überlebt den Board-Server — ohne diese Ernte bliebe der Port
    nach einem Neustart für immer belegt. Aber nur ttyd, nichts anderes."""
    ausgaben = {"lsof": "111\n222\n", 111: "ttyd", 222: "postgres"}

    def _fake_run(cmd, **kw):
        if cmd[0] == "lsof":
            out = ausgaben["lsof"]
        else:
            out = ausgaben[int(cmd[-1])]
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    getoetet = []
    monkeypatch.setattr(terminal, "_run", _fake_run)
    monkeypatch.setattr(terminal.os, "kill", lambda pid, sig: getoetet.append(pid))

    assert terminal.kill_orphan_viewer(47823) == 1
    assert getoetet == [111], "der fremde Prozess auf dem Port bleibt unangetastet"


def test_fehlende_binaries_melden_sich_klar(monkeypatch):
    monkeypatch.setattr(terminal.shutil, "which", lambda name: None)
    with pytest.raises(terminal.TerminalError, match="nicht installiert"):
        terminal.open_terminal("x", "claude", "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")


# ------------------------------------------------------------ Echte Integration


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_tmux_session_ist_idempotent(monkeypatch):
    """Startet wirklich eine tmux-Session — mit `cat`, nicht mit einem Agenten."""
    monkeypatch.setitem(terminal.RESUME, "claude", lambda s: ["cat"])
    item = "pytestterm"
    try:
        terminal.ensure_tmux(item, "claude", "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")
        assert terminal.has_tmux(item)
        terminal.ensure_tmux(item, "claude", "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")
        assert terminal.has_tmux(item)
    finally:
        terminal.kill_tmux(item)
    assert not terminal.has_tmux(item)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_tmux_session_hat_maus_und_keine_statuszeile(monkeypatch):
    """`mouse on` ist der Scroll-Fix (Faden b230272ceb0e): ohne ihn werden Rad-Ticks im
    Browser zu Pfeiltasten. Muss auch eine VORHANDENE Sitzung nachziehen, nicht nur neue."""
    monkeypatch.setitem(terminal.RESUME, "claude", lambda s: ["cat"])
    item = "pytestmouse"
    name = terminal.tmux_name(item)
    try:
        terminal.ensure_tmux(item, "claude", "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")
        terminal._run([terminal.TMUX, "set-option", "-t", name, "mouse", "off"])  # alte Sitzung
        terminal.ensure_tmux(item, "claude", "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")
        out = terminal._run([terminal.TMUX, "display", "-p", "-t", name, "#{mouse} #{status}"])
        assert out.stdout.split() == ["1", "off"]
    finally:
        terminal.kill_tmux(item)


# ------------------------------------------------------------ Endpoint


def _post(port: int, body: dict) -> tuple[int, dict]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/gc-terminal", method="POST",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


@pytest.fixture
def _server(tmp_path, monkeypatch):
    """Echter HTTP-Server auf ephemerem Port, aber ohne echte Prozesse: das
    Terminal-Modul wird an der Grenze ersetzt. Getestet wird die Verdrahtung —
    Item finden, Session lesen, Runner ableiten, Fehler übersetzen."""
    import threading
    from http.server import ThreadingHTTPServer

    import server

    board = tmp_path / "board.md"
    board.write_text(
        "## Thema\n\n### Jetzt\n\n"
        "- [ ] Mit Session *(2026-08-14)*\n"
        "  @gc-id: aaaa11112222\n"
        "  @gc-session: 3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74 · board-test · codex\n"
        "- [ ] Ohne Session *(2026-08-14)*\n"
        "  @gc-id: bbbb33334444\n",
        encoding="utf-8")
    server.Handler.board_path = board
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    gesehen: list = []
    monkeypatch.setattr(terminal, "open_terminal",
                        lambda item, runner, session: gesehen.append((item, runner, session))
                        or {"url": "http://127.0.0.1:47823/", "open": True})
    monkeypatch.setattr(terminal, "close", lambda: {"open": False})
    try:
        yield httpd.server_address[1], gesehen
    finally:
        httpd.shutdown()


def test_endpoint_leitet_runner_und_uuid_aus_der_session_ab(_server):
    port, gesehen = _server
    code, body = _post(port, {"id": "aaaa11112222"})
    assert code == 200 and body["url"].endswith(":47823/")
    assert gesehen == [("aaaa11112222", "codex", "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74")]


def test_endpoint_haerte_gegen_muell_und_leere_items(_server):
    port, _ = _server
    assert _post(port, {"id": "../../etc/passwd"})[0] == 400
    assert _post(port, {"id": "ffffffffffff"})[0] == 409          # kein solches Item
    assert _post(port, {"id": "bbbb33334444"})[0] == 409          # Item ohne Session


def test_endpoint_meldet_fehlende_werkzeuge_als_503(_server, monkeypatch):
    port, _ = _server

    def _boom(*a):
        raise terminal.TerminalError("ttyd ist nicht installiert")

    monkeypatch.setattr(terminal, "open_terminal", _boom)
    code, body = _post(port, {"id": "aaaa11112222"})
    assert code == 503 and "ttyd" in body["error"]


def test_endpoint_verweigert_ein_terminal_auf_einen_laufenden_run(_server):
    """Zwei Prozesse auf einer Session-ID sind der gefährlichste Punkt des Plans."""
    import server
    port, gesehen = _server
    server.RUNNING["aaaa11112222"] = 1.0
    try:
        code, body = _post(port, {"id": "aaaa11112222"})
    finally:
        server.RUNNING.pop("aaaa11112222", None)
    assert code == 409 and "run is active" in body["error"]
    assert gesehen == [], "kein Start, solange der Runner die Session hält"


def test_endpoint_schliesst_ohne_id(_server):
    port, _ = _server
    assert _post(port, {"action": "close"}) == (200, {"open": False})


def test_endpoint_release_und_status(_server, monkeypatch):
    port, _ = _server
    freigegeben = []
    monkeypatch.setattr(terminal, "release",
                        lambda item: freigegeben.append(item) or {"released": True, "open": False})
    monkeypatch.setattr(terminal, "status", lambda: {"open": False})
    monkeypatch.setattr(terminal, "held_items", lambda: ["aaaa11112222"])
    assert _post(port, {"id": "aaaa11112222", "action": "release"})[0] == 200
    assert freigegeben == ["aaaa11112222"]
    assert _post(port, {"action": "release"})[0] == 400, "release braucht eine Id"
    assert _post(port, {"id": "aaaa11112222", "action": "explode"})[0] == 400
    code, body = _post(port, {"action": "status"})
    assert code == 200 and body["held"] == ["aaaa11112222"]


def test_return_to_board_dokumentiert_terminal_arbeit_im_faden(_server, monkeypatch):
    """The handoff is durable board context, not knowledge trapped in the CLI session."""
    import server

    port, _ = _server
    monkeypatch.setattr(terminal, "release",
                        lambda item: {"released": True, "item": item, "open": False})
    launched = []
    monkeypatch.setattr(
        server, "launch_gc_run",
        lambda pending, base_url, claude_cmd, timeout, **kwargs:
            launched.append((pending, kwargs.get("model"))) or True,
    )

    code, body = _post(port, {
        "id": "aaaa11112222", "action": "release", "handoff": True, "model": "",
    })

    assert code == 202 and body["handoff_started"] is True
    board = server.parse_board(server.Handler.board_path.read_text(encoding="utf-8"))
    item = next(it for _s, _n, _c, it in server._all_items(board)
                if it.get("id") == "aaaa11112222")
    turn = item["thread"][-1]
    assert turn["kind"] == "ask" and turn["author"] == "system"
    assert "Summarize what was completed in the terminal" in turn["text"]
    assert launched and launched[0][0]["last_ask"] == turn["text"]


def test_return_to_board_behaelt_handoff_wenn_autostart_blockiert(_server, monkeypatch):
    """Release succeeded even if restart draining blocks auto-resume; ▶ Agent can retry."""
    import server

    port, _ = _server
    monkeypatch.setattr(terminal, "release",
                        lambda item: {"released": True, "item": item, "open": False})
    monkeypatch.setattr(server, "launch_gc_run", lambda *args, **kwargs: False)

    code, body = _post(port, {
        "id": "aaaa11112222", "action": "release", "handoff": True, "model": "",
    })

    assert code == 200 and body["handoff_started"] is False
    board = server.parse_board(server.Handler.board_path.read_text(encoding="utf-8"))
    item = next(it for _s, _n, _c, it in server._all_items(board)
                if it.get("id") == "aaaa11112222")
    assert item["thread"][-1]["text"] == server.TERMINAL_HANDOFF_ASK


def test_run_verweigert_sich_solange_das_terminal_die_session_haelt(_server, monkeypatch):
    """Die andere Hälfte der Besitzregel: hält das Terminal die Session, startet der
    Runner kein `--resume` daneben — mit einer Meldung, die den Ausweg nennt."""
    import server
    port, _ = _server
    monkeypatch.setattr(terminal, "holds_session", lambda item: item == "aaaa11112222")
    # Der erste POST (Terminal hält nicht) startete bis 17.09.2026 einen ECHTEN `claude -p`
    # gegen das Fixture-Item — jeder pytest-Lauf leakte einen Superboard-Run. Gestubbt wird
    # der Prozessstart (run_item), NICHT launch_gc_run: dort sitzt die Besitzregel selbst.
    import time
    import gc_runner
    launched: list = []
    monkeypatch.setattr(gc_runner, "run_item",
                        lambda pending, *a, **kw: launched.append(pending["addr"]["id"]))
    assert server.terminal_holds("aaaa11112222") and not server.terminal_holds("bbbb33334444")
    # laufender Faden mit @gc:-Turn, damit nur der Terminal-Besitz den Start verhindert
    board = server.Handler.board_path
    board.write_text(board.read_text() + "  @gc: mach was\n", encoding="utf-8")
    import json as _json, urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/gc-run",
                                 data=_json.dumps({"id": "bbbb33334444"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    # Das Item mit @gc: ist bbbb…; erst freigeben, dann halten
    try:
        with urllib.request.urlopen(req) as r:
            code_frei = r.status
    except urllib.error.HTTPError as e:
        code_frei = e.code
    monkeypatch.setattr(terminal, "holds_session", lambda item: True)
    try:
        with urllib.request.urlopen(req) as r:
            code, body = r.status, _json.loads(r.read())
    except urllib.error.HTTPError as e:
        code, body = e.code, _json.loads(e.read())
    assert code == 409 and "Return to board" in body["error"], (code_frei, body)
    for _ in range(100):  # der Start läuft im Daemon-Thread — kurz auf den Stub warten
        if launched:
            break
        time.sleep(0.01)
    assert code_frei == 202 and launched == ["bbbb33334444"], (code_frei, launched)


def test_modul_haengt_nicht_am_server():
    """Die Entkopplung ist der Grund, warum das Modul ohne Server testbar ist —
    und warum der laufende OSS-Umbau an server.py ihm nichts anhaben kann."""
    quelle = Path(terminal.__file__).read_text(encoding="utf-8")
    assert "import server" not in quelle
    assert "import gc_runner" not in quelle


# ------------------------------------------------------------ Confirmed input

@pytest.fixture
def input_world(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, 'INPUT_DIR', tmp_path / 'input')
    monkeypatch.setattr(terminal, 'held_items', lambda: ['probe'])
    monkeypatch.setattr(terminal, '_alive', lambda pid: bool(pid))
    from terminal_input import write_json
    write_json(terminal._launch_path('probe'), {'launch': 'launch'})
    return tmp_path / 'input' / 'probe.json'


def test_request_lifecycle_is_protocol_not_idle_or_keystrokes(input_world):
    from terminal_input import RequestState
    state = RequestState(input_world, 'session', 'launch')
    assert terminal.input_states()['probe']['state'] == 'unknown'
    state.observe({'result': {'thread': {'id': 'session'}}})
    assert terminal.input_states()['probe']['state'] == 'ready'
    state.observe({'method': 'item/agentMessage/delta', 'params': {'threadId': 'session', 'delta': 'Permission required'}})
    assert terminal.input_states()['probe']['state'] == 'ready'
    state.observe({'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'other'}})
    assert terminal.input_states()['probe']['state'] == 'ready'
    state.observe({'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'session'}})
    first = terminal.input_states()['probe']
    assert first['state'] == 'waiting' and first['seen'] is False
    assert terminal.acknowledge('probe', first['request_id'])
    assert terminal.input_states()['probe']['seen'] is True
    assert terminal.input_states()['probe']['state'] == 'waiting'
    # The client response, including a rejected answer, cannot clear server state.
    state.observe({'id': 1, 'result': {'decision': 'decline'}})
    assert terminal.input_states()['probe']['state'] == 'waiting'
    state.observe({'method': 'serverRequest/resolved', 'params': {'threadId': 'session', 'requestId': 1}})
    assert terminal.input_states()['probe']['state'] == 'ready'
    state.observe({'id': 2, 'method': 'item/tool/requestUserInput', 'params': {'threadId': 'session'}})
    second = terminal.input_states()['probe']
    assert second['state'] == 'waiting' and second['seen'] is False
    assert second['request_id'] != first['request_id']
    assert not terminal.acknowledge('probe', first['request_id'])
    state.observe({'method': 'turn/completed', 'params': {'threadId': 'session'}})
    assert terminal.input_states()['probe']['state'] == 'ready'
    state.unavailable('ended', ended=True)
    assert terminal.input_states()['probe']['state'] == 'ended'


def test_parallel_requests_clear_only_matching_identity(input_world):
    from terminal_input import RequestState
    state = RequestState(input_world, 'session', 'launch')
    for identity in [1, '1']:
        state.observe({'id': identity, 'method': 'item/fileChange/requestApproval', 'params': {'threadId': 'session'}})
    first = terminal.input_states()['probe']['request_id']
    state.observe({'method': 'serverRequest/resolved', 'params': {'threadId': 'session', 'requestId': 1}})
    assert terminal.input_states()['probe']['state'] == 'waiting'
    assert terminal.input_states()['probe']['request_id'] != first
    state.unavailable('connection lost')
    assert terminal.input_states()['probe']['state'] == 'unknown'
    assert terminal.input_states()['probe']['request_id'] is None


def test_input_snapshot_survives_reader_reload_and_stale_pid_clears(input_world, monkeypatch):
    from terminal_input import RequestState, read_json, write_json
    state = RequestState(input_world, 'session', 'launch')
    state.observe({'id': 'request', 'method': 'item/permissions/requestApproval', 'params': {'threadId': 'session'}})
    assert terminal.input_states()['probe']['state'] == 'waiting'
    # No in-memory registry is needed by the Board after its own restart.
    del state
    assert terminal.input_states()['probe']['state'] == 'waiting'
    snapshot = read_json(input_world)
    write_json(input_world, {**snapshot, 'updated': 0})
    assert terminal.input_states()['probe']['state'] == 'unknown'
    write_json(input_world, snapshot)
    monkeypatch.setattr(terminal, '_alive', lambda pid: False)
    assert terminal.input_states()['probe']['state'] == 'unknown'
    monkeypatch.setattr(terminal, 'held_items', lambda: [])
    assert terminal.input_states() == {}


def test_legacy_and_unsupported_terminals_explicitly_unknown(input_world):
    from terminal_input import write_json
    assert terminal.input_states()['probe']['state'] == 'unknown'
    for runner in ['claude', 'opencode']:
        write_json(input_world, {'state': 'unknown', 'runner': runner, 'reason': 'Input detection unavailable for this runner'})
        assert terminal.input_states()['probe']['runner'] == runner
        assert terminal.input_states()['probe']['state'] == 'unknown'
        assert not terminal.acknowledge('probe', 'anything')


def test_launch_identity_prevents_seen_leaking_to_next_terminal(input_world):
    from terminal_input import RequestState, write_json
    write_json(terminal._launch_path('probe'), {'launch': 'first-launch'})
    first = RequestState(input_world, 'session', 'first-launch')
    event = {'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'session'}}
    first.observe(event)
    assert terminal.acknowledge('probe', terminal.input_states()['probe']['request_id'])
    write_json(terminal._launch_path('probe'), {'launch': 'second-launch'})
    second = RequestState(input_world, 'session', 'second-launch')
    second.observe(event)
    assert terminal.input_states()['probe']['seen'] is False


def test_old_observer_cannot_overwrite_new_launch(input_world):
    from terminal_input import RequestState, write_json
    write_json(terminal._launch_path('probe'), {'launch': 'new-launch'})
    old = RequestState(input_world, 'session', 'old-launch')
    old.observe({'id': 1, 'method': 'item/commandExecution/requestApproval',
                 'params': {'threadId': 'session'}})
    assert terminal.input_states()['probe']['state'] == 'unknown'
    current = RequestState(input_world, 'session', 'new-launch')
    current.observe({'result': {'thread': {'id': 'session'}}})
    assert terminal.input_states()['probe']['state'] == 'ready'


def test_ready_snapshot_without_live_observer_is_unknown(input_world):
    from terminal_input import write_json
    write_json(input_world, {'state': 'ready', 'runner': 'codex', 'reason': 'stale'})
    assert terminal.input_states()['probe']['state'] == 'unknown'


def test_codex_observer_launch_uses_config_or_env_and_preserves_runner_config(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, 'INPUT_DIR', tmp_path)
    monkeypatch.setattr(terminal.shutil, 'which', lambda _: '/fake/codex')
    monkeypatch.setattr(terminal, 'has_tmux', lambda _: False)
    monkeypatch.setattr(terminal, '_run', lambda cmd, **kw: calls.append(cmd)
                        or subprocess.CompletedProcess(cmd, 0, '', ''))
    session = '3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74'
    monkeypatch.setenv('GC_TERM_CODEX_INPUT', '0')
    terminal.ensure_tmux('direct', 'codex', session)
    direct = next(c for c in calls if 'new-session' in c)
    assert 'terminal_input.py' not in direct[-1]
    assert f'codex resume {session}' in direct[-1]
    calls.clear()
    monkeypatch.setenv('GC_TERM_CODEX_INPUT', '1')
    terminal.ensure_tmux('observed', 'codex', session)
    observed = next(c for c in calls if 'new-session' in c)
    assert 'terminal_input.py' in observed[-1]
    assert f'--session {session}' in observed[-1]
    assert 'CODEX_HOME=' + terminal.RUNNER_ENV['codex']['CODEX_HOME'] in observed
    assert '--sandbox' not in observed[-1] and '--ask-for-approval' not in observed[-1]


def test_codex_observer_is_enabled_by_local_config(monkeypatch):
    monkeypatch.delenv('GC_TERM_CODEX_INPUT', raising=False)
    monkeypatch.setattr(terminal.board_config, 'CODEX_TERMINAL_INPUT', True)
    assert terminal._codex_input_enabled() is True
    monkeypatch.setattr(terminal.board_config, 'CODEX_TERMINAL_INPUT', False)
    assert terminal._codex_input_enabled() is False


def test_socket_first_normal_quit_does_not_reopen_terminal():
    import asyncio
    from terminal_input import observer_failed

    async def scenario():
        loop = asyncio.get_running_loop()
        tui = loop.create_future()
        server = loop.create_future()
        disconnected = loop.create_future()
        disconnected.set_result(True)
        loop.call_later(0.001, tui.set_result, 0)
        assert not await observer_failed({disconnected}, tui, server, disconnected, 0.05)
        server.cancel()

    asyncio.run(scenario())


def test_broken_observer_transport_falls_back_when_tui_stays_open():
    import asyncio
    from terminal_input import observer_failed

    async def scenario():
        loop = asyncio.get_running_loop()
        tui = loop.create_future()
        server = loop.create_future()
        disconnected = loop.create_future()
        disconnected.set_result(True)
        assert await observer_failed({disconnected}, tui, server, disconnected, 0.001)
        tui.cancel()
        server.cancel()

    asyncio.run(scenario())


def test_terminal_open_and_runner_reservation_are_atomic(_server, monkeypatch):
    """A runner arriving during terminal creation waits, then sees terminal ownership."""
    import server
    import gc_runner
    port, _ = _server
    entered, finish_open, attempted, run_done = (threading.Event() for _ in range(4))
    held = set()
    result = []
    monkeypatch.setattr(server, 'RUNNING', {})
    monkeypatch.setattr(server, 'BEATS', {})
    monkeypatch.setattr(server, 'restart_draining', lambda: False)
    monkeypatch.setattr(server, 'terminal_holds', lambda item: item in held)
    monkeypatch.setattr(gc_runner, 'run_item', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_maybe_retrigger', lambda *a, **kw: None)
    def open_terminal(item, *args):
        entered.set()
        assert finish_open.wait(3)
        held.add(item)
        return {'open': True}
    monkeypatch.setattr(terminal, 'open_terminal', open_terminal)
    terminal_call = threading.Thread(target=lambda: _post(port, {'id': 'aaaa11112222'}))
    def launch():
        attempted.set()
        result.append(server.launch_gc_run({'addr': {'id': 'aaaa11112222'}},
                                          'http://127.0.0.1', 'unused', 10))
        run_done.set()
    contender = threading.Thread(target=launch)
    terminal_call.start()
    try:
        assert entered.wait(2)
        contender.start()
        assert attempted.wait(2)
        assert not run_done.wait(0.1)
    finally:
        finish_open.set()
        terminal_call.join(3)
        if contender.ident is not None:
            contender.join(3)
    assert not terminal_call.is_alive() and not contender.is_alive()
    assert result == [False]
