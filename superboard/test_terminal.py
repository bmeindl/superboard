"""Terminal-Sidecar: Eingabe-Härtung, Ein-Betrachter-Regel, Lebenszyklus.

Die teuren Teile (tmux startet wirklich, ttyd bindet wirklich) laufen als echte
Integration, wenn die Binaries da sind — mit `cat` statt `claude`, damit kein
Agent gestartet wird. Alles andere läuft gegen Doubles, weil sonst jeder Lauf
Prozesse hinterlässt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import terminal


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "STATE_PATH", tmp_path / "state.json")


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


def test_zweites_item_haengt_den_einen_betrachter_um(monkeypatch):
    lebende, gestartet = set(), []
    getoetet = _fake_welt(monkeypatch, lebende, gestartet)
    uuid = "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74"

    erst = terminal.open_terminal("itemeins", "claude", uuid)
    zweit = terminal.open_terminal("itemzwei", "codex", uuid)

    assert erst["port"] == zweit["port"] == terminal.PORT
    assert getoetet == [erst["ttyd_pid"]], "der alte Betrachter muss weichen"
    assert len(gestartet) == 2
    assert gestartet[1][-4:] == ["attach", "-r", "-t", "gcterm-itemzwei"]
    assert "-W" not in gestartet[1], "read-only ist keine Option, sondern die Regel"
    assert "127.0.0.1" in gestartet[1], "kein offener Port nach außen"
    assert json.loads(terminal.STATE_PATH.read_text())["item"] == "itemzwei"


def test_dasselbe_item_startet_nichts_neu(monkeypatch):
    lebende, gestartet = set(), []
    _fake_welt(monkeypatch, lebende, gestartet)
    uuid = "3a4dadb6-2b03-40df-bbb9-2aa2a92d1f74"

    terminal.open_terminal("itemeins", "claude", uuid)
    wieder = terminal.open_terminal("itemeins", "claude", uuid)

    assert wieder["reused"] is True
    assert len(gestartet) == 1


def test_status_raeumt_verwaisten_zustand_auf(monkeypatch):
    terminal.STATE_PATH.write_text(json.dumps({"item": "alt", "ttyd_pid": 1}))
    monkeypatch.setattr(terminal, "_alive", lambda pid: False)

    assert terminal.status() == {"open": False}
    assert terminal.STATE_PATH.read_text() == "{}"


def test_close_beendet_den_betrachter_und_laesst_tmux_stehen(monkeypatch):
    lebende, gestartet = {77}, []
    _fake_welt(monkeypatch, lebende, gestartet)
    terminal.STATE_PATH.write_text(json.dumps({"item": "x", "ttyd_pid": 77}))
    kill_aufrufe = []
    monkeypatch.setattr(terminal, "kill_tmux", lambda item: kill_aufrufe.append(item))

    assert terminal.close() == {"open": False}
    assert kill_aufrufe == [], "der Zustand des Items überlebt das Schließen"


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


def test_modul_haengt_nicht_am_server():
    """Die Entkopplung ist der Grund, warum das Modul ohne Server testbar ist —
    und warum der laufende OSS-Umbau an server.py ihm nichts anhaben kann."""
    quelle = Path(terminal.__file__).read_text(encoding="utf-8")
    assert "import server" not in quelle
    assert "import gc_runner" not in quelle
