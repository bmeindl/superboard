"""Terminal-Sidecar: eine read-only Web-Ansicht der Agenten-Session eines Items.

Das Board zeigt Runs bisher als Ereignis-Strom (Journal → SSE-Panel). Diese
Ansicht ist die zweite Sicht daneben: das echte Terminal derselben Session, so
wie es im iTerm aussähe. Sie ist ausdrücklich ein **Resume-Terminal**, kein
Spiegel eines laufenden Prozesses — `claude --resume <uuid>` bzw.
`codex resume <uuid>` startet eine neue Anzeige AUF derselben Session und
rendert deren Historie. Ein gerade laufender Board-Run wird davon nicht
gespiegelt (und darf, solange nur gelesen wird, auch nicht gestört werden).

Aufbau, bewusst aus zwei Fertigbausteinen statt Eigenbau mit xterm.js:

    tmux-Session `gcterm-<item>`   hält den Agenten-Prozess, überlebt Reload
      └─ ttyd auf 127.0.0.1:47823  rendert genau EINE davon im Browser

Zwei Invarianten, die den Rest erklären:

* **Genau ein Betrachter.** Ein einziger ttyd auf einem festen Port; beim
  Item-Wechsel hängt er um. Die tmux-Sessions bleiben pro Item bestehen, der
  Zustand geht also nicht verloren. Der Grund ist nicht Bequemlichkeit, sondern
  der Lebenszyklus: n Betrachter wären n Ports, n Prozesspaare und ab dem
  Schreibmodus n potenzielle Schreiber auf n Session-IDs.
* **Read-only, doppelt.** ttyd läuft ohne `-W` (Default ist nicht schreibbar)
  UND hängt sich mit `tmux attach -r` an. Ein Schreibmodus ist eine eigene,
  spätere Entscheidung; er braucht ein Item-Lock, weil zwei Schreiber auf einer
  Session-ID deren Verlauf zerlegen.

Bindung an 127.0.0.1: der Port hat keine Auth. Read-only begrenzt den Schaden
auf Mitlesen durch lokale Prozesse — das ist der Grund, warum der Schreibmodus
nicht nebenbei mitkommt.

Dieses Modul kennt weder board.md noch das Item-Dict: es bekommt Item-Id,
Runner und Session-UUID gereicht. Damit ist es ohne laufenden Board-Server
testbar und per CLI (`python3 terminal.py open …`) allein benutzbar.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import paths as _p
from claude_identity import default_shell_prelude

GC_ROOT = _p.GC_ROOT

TMUX = os.environ.get("GC_TMUX_BIN", "tmux")
TTYD = os.environ.get("GC_TTYD_BIN", "ttyd")

# Fester Port, weil es genau einen Betrachter gibt. Env-Override macht Tests und
# eine zweite Instanz auf derselben Maschine billig.
PORT = int(os.environ.get("GC_TERM_PORT", "47823"))

# Wer gerade gezeigt wird, überlebt einen Server-Neustart — sonst bliebe ein
# verwaister ttyd auf dem Port liegen, den niemand mehr zuordnen kann.
STATE_PATH = Path(
    os.environ.get("GC_TERM_STATE", str(Path(tempfile.gettempdir()) / "gc-terminal.json"))
)

_ITEM_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Der Startbefehl je Runner. Die Formen sind gemessen, nicht aus der Doku:
# `claude --resume <uuid>` startet in tmux ohne Auth-Rückfrage und rendert die
# volle Historie; `codex resume <uuid>` ist die symmetrische Form.
_CLAUDE_WRAPPER = GC_ROOT / "tools" / "claude-identities" / "claude-private"
_CLAUDE = str(_CLAUDE_WRAPPER) if _CLAUDE_WRAPPER.is_file() else "claude"
# Codex liegt NICHT im PATH (gemessen: `command not found`), sondern in der App —
# derselbe Default und derselbe Env-Name wie in gc_runner.CODEX_CMD.
_CODEX = os.environ.get("GC_RUNNER_CODEX", "/Applications/ChatGPT.app/Contents/Resources/codex")
RESUME = {
    "claude": lambda s: [_CLAUDE, "--resume", s],
    "codex": lambda s: [_CODEX, "resume", s],
}

# Board runs keep a dedicated Codex store in the workspace runtime directory.
RUNNER_ENV = {"codex": {"CODEX_HOME": str(_p.DATA / "codex-home")}}


class TerminalError(RuntimeError):
    """Vorhersehbarer Fehlschlag — wird als 4xx/5xx-Meldung an die UI gereicht."""


# ---------------------------------------------------------------- Bausteine


def tmux_name(item_id: str) -> str:
    """Session-Name pro Item. `.` und `:` sind in tmux-Namen Sonderzeichen,
    deshalb die enge Id-Prüfung statt eines Escape-Versuchs."""
    if not _ITEM_RE.match(item_id or ""):
        raise TerminalError(f"unbrauchbare Item-Id: {item_id!r}")
    return f"gcterm-{item_id}"


def resume_cmd(runner: str, session: str) -> list[str]:
    """Der Befehl, der IN der tmux-Session läuft."""
    if runner not in RESUME:
        raise TerminalError(f"unbekannter Runner: {runner!r}")
    if not _UUID_RE.match(session or ""):
        raise TerminalError(f"unbrauchbare Session-UUID: {session!r}")
    return RESUME[runner](session)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20, **kw)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def has_tmux(item_id: str) -> bool:
    return _run([TMUX, "has-session", "-t", tmux_name(item_id)]).returncode == 0


def ensure_tmux(item_id: str, runner: str, session: str) -> str:
    """tmux-Session für das Item starten, falls sie nicht schon läuft.

    Idempotent: eine bestehende Session wird NICHT neu gestartet — sie ist der
    Zustand, den der Betrachter beim Umhängen wiederfindet."""
    name = tmux_name(item_id)
    if has_tmux(item_id):
        return name
    cmd = resume_cmd(runner, session)
    if not shutil.which(cmd[0]):
        raise TerminalError(f"{cmd[0]} nicht gefunden — Runner {runner!r} nicht startbar")

    # Der Befehl läuft in einer Shell, die die Pane nach dem Ende offen hält. Ohne
    # das verschwindet die tmux-Session bei jedem Fehlschlag wortlos (gemessen an
    # `codex resume` ohne CODEX_HOME) und der Betrachter zeigt nur Leere — die
    # Fehlermeldung ist aber genau das, was man dann sehen will.
    inner = " ".join(shlex.quote(part) for part in cmd)
    if runner == "claude":
        # tmux has its own long-lived environment, so enforce the same default-account
        # boundary inside the pane's shell as in headless runs.
        inner = default_shell_prelude() + inner
    halten = f'{inner}; printf "\\n[gc] session ended (exit %s)\\n" $?; read -r _'

    env_args = []
    for key, val in RUNNER_ENV.get(runner, {}).items():
        env_args += ["-e", f"{key}={val}"]

    # Feste Startgröße: ohne angehängten Client fällt tmux auf 80x24 zurück und
    # die Historie würde für diese Breite umgebrochen, bevor der Browser da ist.
    res = _run(
        [TMUX, "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
         "-c", str(GC_ROOT), *env_args, "sh", "-c", halten]
    )
    if res.returncode != 0:
        raise TerminalError(f"tmux-Start fehlgeschlagen: {res.stderr.strip() or res.stdout.strip()}")
    # Die tmux-Statuszeile ist im Board nur Rauschen: sie zeigt Fensternamen und Uhrzeit
    # in einem Panel, das schon weiß, welches Item es zeigt. Fehlschlag ist egal.
    _run([TMUX, "set-option", "-t", name, "status", "off"])
    return name


def kill_tmux(item_id: str) -> bool:
    """Die Session eines Items wirklich beenden (nicht Teil des Schließens —
    das Panel lässt sie absichtlich stehen)."""
    return _run([TMUX, "kill-session", "-t", tmux_name(item_id)]).returncode == 0


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _stop_viewer(state: dict) -> None:
    pid = state.get("ttyd_pid")
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):                      # ~1 s auf den Port warten
            if not _alive(pid):
                break
            time.sleep(0.05)


def kill_orphan_viewer(port: int = PORT) -> int:
    """Verwaisten ttyd auf unserem Port einsammeln. Nötig, weil der Betrachter den
    Board-Server bewusst überlebt (eigene Prozessgruppe) — geht die Zustandsdatei
    verloren (Neustart, /tmp-Aufräumen), hält er den Port und der Knopf meldete nur
    noch „Port belegt". Beendet wird NUR ein Prozess, der wirklich ttyd heißt: alles
    andere auf diesem Port ist fremd und geht uns nichts an. Rückgabe: Anzahl."""
    res = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
    getroffen = 0
    for zeile in res.stdout.split():
        try:
            pid = int(zeile)
        except ValueError:
            continue
        name = _run(["ps", "-o", "comm=", "-p", str(pid)]).stdout.strip()
        if "ttyd" not in name:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            getroffen += 1
        except OSError:
            pass
    return getroffen


def close() -> dict:
    """Betrachter beenden, tmux-Sessions bewusst stehen lassen."""
    state = _read_state()
    _stop_viewer(state)
    _write_state({})
    return {"open": False}


def open_terminal(item_id: str, runner: str, session: str) -> dict:
    """tmux sicherstellen, den einen Betrachter dorthin umhängen, URL liefern."""
    for binary in (TMUX, TTYD):
        if not shutil.which(binary):
            raise TerminalError(f"{binary} ist nicht installiert (brew install {binary})")

    state = _read_state()
    # Nur wiederverwenden, wenn BEIDES lebt: ein Betrachter auf einer toten
    # tmux-Session ist ein leeres Fenster, das wie ein Bug aussieht (gemessen).
    if (state.get("item") == item_id and _alive(state.get("ttyd_pid"))
            and has_tmux(item_id)):
        return {**state, "open": True, "reused": True}

    ensure_tmux(item_id, runner, session)
    _stop_viewer(state)                          # genau ein Betrachter

    for versuch in range(2):
        for _ in range(40):                      # der alte Port braucht einen Moment
            if _port_free(PORT):
                break
            time.sleep(0.05)
        else:
            if versuch == 0 and kill_orphan_viewer(PORT):
                continue                         # verwaister ttyd, jetzt nochmal
            raise TerminalError(f"Port {PORT} bleibt belegt — fremder Prozess?")
        break

    proc = subprocess.Popen(
        [TTYD, "-p", str(PORT), "-i", "127.0.0.1",
         "-t", "disableLeaveAlert=true", "-t", "titleFixed=" + tmux_name(item_id),
         TMUX, "attach", "-r", "-t", tmux_name(item_id)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,                  # überlebt das Ende des Board-Runs
    )
    for _ in range(60):                          # ttyd bindet in ~2 s
        if not _port_free(PORT):
            break
        if proc.poll() is not None:
            raise TerminalError("ttyd hat sich sofort beendet")
        time.sleep(0.05)
    else:
        proc.terminate()
        raise TerminalError("ttyd hat den Port nicht gebunden")

    new = {
        "item": item_id, "runner": runner, "session": session,
        "ttyd_pid": proc.pid, "port": PORT,
        "url": f"http://127.0.0.1:{PORT}/", "tmux": tmux_name(item_id),
    }
    _write_state(new)
    return {**new, "open": True, "reused": False}


def status() -> dict:
    """Was die UI wissen muss: läuft ein Betrachter, und für welches Item."""
    state = _read_state()
    if not _alive(state.get("ttyd_pid")):
        if state:
            _write_state({})
        return {"open": False}
    return {**state, "open": True, "tmux_alive": has_tmux(state.get("item", ""))}


# ---------------------------------------------------------------- CLI


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("usage: terminal.py open <item-id> <claude|codex> <session-uuid>")
        print("       terminal.py close | status | kill <item-id>")
        return 0
    cmd, *rest = argv
    try:
        if cmd == "open":
            out = open_terminal(*rest)
        elif cmd == "close":
            out = close()
        elif cmd == "status":
            out = status()
        elif cmd == "kill":
            out = {"killed": kill_tmux(rest[0])}
        else:
            raise TerminalError(f"unbekannter Befehl: {cmd}")
    except (TerminalError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
