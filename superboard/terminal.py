"""Terminal-Sidecar: die Agenten-Session eines Items als Web-Terminal — schreibbar.

Das Board zeigt Runs bisher als Ereignis-Strom (Journal → SSE-Panel). Diese
Ansicht ist die zweite Sicht daneben: das echte Terminal derselben Session, so
wie es im iTerm aussähe. Sie ist ausdrücklich ein **Resume-Terminal**, kein
Spiegel eines laufenden Prozesses — `claude --resume <uuid>` bzw.
`codex resume <uuid>` startet eine neue Anzeige AUF derselben Session und
rendert deren Historie. Ein gerade laufender Board-Run wird davon nicht
gespiegelt (und darf, solange nur gelesen wird, auch nicht gestört werden).

Aufbau, bewusst aus zwei Fertigbausteinen statt Eigenbau mit xterm.js:

    tmux-Session `gcterm-<item>`   hält den Agenten-Prozess, überlebt Reload — EINE pro Item
      └─ ttyd auf 127.0.0.1:47823  EIN gemeinsamer Betrachter, der per URL-Argument
                                   (`?arg=gcterm-<item>`, ttyd `-a`) jede davon zeigt

Zwei Invarianten, die den Rest erklären:

* **Ein Betrachter-Prozess, beliebig viele Sitzungen.** Bis 2026-09-07 hing der eine
  ttyd fest an EINER tmux-Session und wurde beim Item-Wechsel umgehängt — für the owner
  sah das aus wie „es gibt nur ein Terminal" (Faden a410c2f3fffe). Jetzt startet ttyd
  mit `-a` und einem winzigen Attach-Skript: der Browser sagt im Query-String, welche
  `gcterm-*`-Sitzung er sehen will, das Skript prüft den Namen streng und hängt sich
  dort an. Ein Port, ein Prozess, n Iframes — jedes Item hat sein eigenes Terminal,
  und mehrere gleichzeitig sind kein Sonderfall mehr. Die Sitzungen bleiben pro Item
  bestehen, egal welches Overlay gerade offen ist.
* **Schreibbar, mit Besitz.** Seit 2026-09-06 (the owner: „aktuell kann ich nichts
  eingeben") läuft ttyd mit `-W` und der Attach ohne `-r`: man tippt direkt in die
  Session — Rechte erteilen, `!aws sso login`, `/`-Kommandos. Bis dahin war die
  Ansicht doppelt read-only, weil zwei Schreiber auf einer Session-ID deren
  Verlauf zerlegen. Genau das fängt jetzt die *Besitzregel* ab: solange die
  tmux-Session eines Items lebt, „hält" das Terminal die Session und der
  Board-Runner darf sie nicht mit `--resume` anfassen (`holds_session`, vom
  Server vor jedem Run geprüft); umgekehrt verweigert der Server das Terminal,
  solange ein Run läuft. `release()` gibt die Session zurück (tmux killen) — das
  ist der Knopf „Return to board" im Panel. Zwei Iframes auf DERSELBEN Sitzung
  (zwei Board-Tabs) sind zwei tmux-Clients auf einer Sitzung — tmux spiegelt, es
  entsteht kein zweiter Schreiber auf der Session-ID.

Bindung an 127.0.0.1: der Port hat keine Auth. Ein lokaler Prozess, der ihn
findet, kann in die Session tippen — aber derselbe Prozess kann am Board-Server
(ebenfalls ohne Auth, 47822) ohnehin Runs mit Auto-Permissions starten; der
Vertrauensraum wird also nicht weiter, nur breiter sichtbar. `-O` (check origin)
hält zusätzlich fremde Browser-Seiten vom WebSocket fern; die Sitzung stirbt mit
dem Knopf, nicht mit dem Tab.

Dieses Modul kennt weder board.md noch das Item-Dict: es bekommt Item-Id,
Runner und Session-UUID gereicht. Damit ist es ohne laufenden Board-Server
testbar und per CLI (`python3 terminal.py open …`) allein benutzbar.
"""

from __future__ import annotations

import hashlib
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
import uuid
from pathlib import Path

import config as board_config
from terminal_input import read_json, write_json
import paths as _p
from claude_identity import default_shell_prelude

GC_ROOT = _p.GC_ROOT

TMUX = os.environ.get("GC_TMUX_BIN", "tmux")
TTYD = os.environ.get("GC_TTYD_BIN", "ttyd")

# Fester Port, weil es genau einen Betrachter gibt. Env-Override macht Tests und
# eine zweite Instanz auf derselben Maschine billig.
# Distinct installations must not reuse a viewer or tmux server.
_WORKSPACE_KEY = hashlib.sha256(str(_p.GC_ROOT.resolve()).encode()).hexdigest()[:8]
_DEFAULT_PORT = 49152 + int(_WORKSPACE_KEY, 16) % 15000
PORT = int(os.environ.get("GC_TERM_PORT", str(_DEFAULT_PORT)))
TMUX_DIR = Path(tempfile.gettempdir()) / f"sbterm-{os.getuid()}-{_WORKSPACE_KEY}"

# Der Betrachter-Prozess überlebt einen Server-Neustart — sonst bliebe ein
# verwaister ttyd auf dem Port liegen, den niemand mehr zuordnen kann. Seit 2026-09-07
# steht hier kein Item mehr: der eine ttyd zeigt alle Sitzungen, die Auswahl trifft
# der Browser per URL.
STATE_PATH = Path(
    os.environ.get("GC_TERM_STATE", str(_p.DATA / "terminal.json"))
)

# Separate from viewer ownership: each terminal observer owns one small snapshot.
INPUT_DIR = Path(os.environ.get("GC_TERM_INPUT_DIR", str(_p.DATA / "terminal-input")))


def _input_path(item_id: str) -> Path:
    tmux_name(item_id)  # same strict validation as the terminal owner
    return INPUT_DIR / (item_id + ".json")


def _launch_path(item_id: str) -> Path:
    tmux_name(item_id)
    return INPUT_DIR / (item_id + ".launch.json")


def _codex_input_enabled() -> bool:
    override = os.environ.get("GC_TERM_CODEX_INPUT")
    return override == "1" if override is not None else board_config.CODEX_TERMINAL_INPUT


def input_states() -> dict[str, dict]:
    """Only live terminal owners participate; no transcript/idle-time inference."""
    states = {}
    for item in held_items():
        state = read_json(_input_path(item))
        owner = read_json(_launch_path(item))
        if state.get("state") not in {"unknown", "ready", "waiting", "ended"}:
            state = {}
        if state.get("state") in {"ready", "waiting"} and owner.get("launch") != state.get("launch"):
            state = {}
        if not state:
            state = {"state": "unknown", "runner": None, "reason": "Input detection unavailable for this terminal"}
        elif state.get("state") in {"ready", "waiting"} and (
            not isinstance(state.get("bridge_pid"), int)
            or not isinstance(state.get("updated"), (int, float))
            or not _alive(state["bridge_pid"]) or time.time() - state["updated"] > 15
        ):
            state = {**state, "state": "unknown", "reason": "Input observer unavailable", "request_id": None, "since": None}
        ack = read_json(INPUT_DIR / (item + ".seen.json"))
        request = state.get("request_id")
        states[item] = {key: state.get(key) for key in ("state", "request_id", "reason", "since", "runner")}
        states[item]["seen"] = bool(request and ack.get("request_id") == request)
    return states


def acknowledge(item_id: str, request_id: str) -> bool:
    """Opening one prompt marks only that request seen, never answers it."""
    tmux_name(item_id)
    current = input_states().get(item_id, {})
    if not request_id or current.get("state") != "waiting" or current.get("request_id") != request_id:
        return False
    write_json(INPUT_DIR / (item_id + ".seen.json"), {"request_id": request_id})
    return True

_ITEM_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Der Startbefehl je Runner. Die Formen sind gemessen, nicht aus der Doku:
# `claude --resume <uuid>` startet in tmux ohne Auth-Rückfrage und rendert die
# volle Historie; `codex resume <uuid>` ist die symmetrische Form.
_CLAUDE_WRAPPER = GC_ROOT / "tools" / "claude-identities" / "claude-private"
_CLAUDE = str(_CLAUDE_WRAPPER) if _CLAUDE_WRAPPER.is_file() else "claude"
# Codex liegt NICHT im PATH (gemessen: `command not found`), sondern in der App —
# dieselbe Auflösung (Env, App-Kandidaten, PATH) wie gc_runner.codex_cmd().
_CODEX_CANDIDATES = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",  # ab ChatGPT 26.928
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)


def _codex() -> str:
    env = os.environ.get("GC_RUNNER_CODEX")
    if env:
        return env
    for cand in _CODEX_CANDIDATES:
        if os.access(cand, os.X_OK):
            return cand
    return shutil.which("codex") or _CODEX_CANDIDATES[0]


RESUME = {
    "claude": lambda s: [_CLAUDE, "--resume", s],
    "codex": lambda s: [_codex(), "resume", s],
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


# Was ttyd pro Browser-Verbindung startet. Das Argument kommt aus der URL (`-a`), also
# aus dem Browser — deshalb der strenge `case`: NUR unsere `gcterm-*`-Sitzungen, sonst
# könnte eine fremde Seite (oder ein Tippfehler) sich an irgendeine tmux-Sitzung des
# Benutzers hängen. Weitere Argumente werden ignoriert, ein fehlendes wird benannt.
ATTACH_SCRIPT = (
    'export TMUX_TMPDIR=' + shlex.quote(str(TMUX_DIR)) + '; '
    'case "$1" in gcterm-*) item=${1#gcterm-}; '
    'case "$item" in ""|*[!A-Za-z0-9_-]*) ;; *) '
    '[ "${#item}" -le 40 ] && exec ' + shlex.quote(TMUX) + ' attach -t "$1";; esac;; '
    'esac; printf "\\n[gc] no session named %s\\n" "$1"; read -r _'
)


def url_for(item_id: str, port: int = PORT) -> str:
    """Die Iframe-URL eines Items: der eine Betrachter plus das URL-Argument."""
    return f"http://127.0.0.1:{port}/?arg={tmux_name(item_id)}"


def resume_cmd(runner: str, session: str) -> list[str]:
    """Der Befehl, der IN der tmux-Session läuft."""
    if runner not in RESUME:
        raise TerminalError(f"unbekannter Runner: {runner!r}")
    if not _UUID_RE.match(session or ""):
        raise TerminalError(f"unbrauchbare Session-UUID: {session!r}")
    return RESUME[runner](session)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    if cmd and cmd[0] == TMUX:
        TMUX_DIR.mkdir(parents=True, exist_ok=True)
        kw["env"] = {**os.environ, **kw.pop("env", {}), "TMUX_TMPDIR": str(TMUX_DIR)}
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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def has_tmux(item_id: str) -> bool:
    return _run([TMUX, "has-session", "-t", tmux_name(item_id)]).returncode == 0


def ensure_tmux(item_id: str, runner: str, session: str) -> str:
    """tmux-Session für das Item starten, falls sie nicht schon läuft.

    Idempotent: eine bestehende Session wird NICHT neu gestartet — sie ist der
    Zustand, den der Betrachter beim Umhängen wiederfindet."""
    name = tmux_name(item_id)
    if has_tmux(item_id):
        _tune_session(name)                      # ältere Sitzungen nachziehen
        return name
    cmd = resume_cmd(runner, session)
    if not shutil.which(cmd[0]):
        raise TerminalError(f"{cmd[0]} nicht gefunden — Runner {runner!r} nicht startbar")

    launch = uuid.uuid4().hex
    observe_input = runner == "codex" and _codex_input_enabled()
    if observe_input:
        try:
            import websockets.asyncio.server  # noqa: F401 -- verify before handing ownership over
        except ImportError as exc:
            raise TerminalError("Codex input detection requires Python package websockets (>=14)") from exc
        cmd = [sys.executable, str(Path(__file__).with_name("terminal_input.py")),
               "--codex", cmd[0], "--session", session,
               "--state", str(_input_path(item_id)), "--launch", launch]
    write_json(_launch_path(item_id), {"launch": launch})
    write_json(_input_path(item_id), {"state": "unknown", "runner": runner,
               "session": session, "launch": launch,
               "reason": "Input observer starting" if observe_input else "Input detection unavailable for this terminal"})

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
    _tune_session(name)
    return name


def _tune_session(name: str) -> None:
    """Sitzungsoptionen, die das Terminal im Browser brauchbar machen. Idempotent,
    Fehlschlag ist egal (dann fehlt Komfort, nicht die Sitzung).

    `status off`: die tmux-Statuszeile ist im Board nur Rauschen — Fensternamen und
    Uhrzeit in einem Panel, das schon weiß, welches Item es zeigt.

    `mouse on`: ohne das kommt Trackpad-Scrollen beim Agenten als Pfeiltasten an.
    tmux läuft im alternativen Bildschirm, und xterm.js (in ttyd) übersetzt dort
    Mausrad-Ereignisse in ↑/↓ — Claude Code blättert dann durch seine Eingabe-Historie
    statt den Verlauf zu zeigen (the owner, Faden b230272ceb0e; gemessen: fünf Rad-Ticks =
    40× `^[[A` in der Pane). Mit `mouse on` fängt tmux das Rad selbst, scrollt im
    Copy-Mode durch die Historie und lässt Tasten Tasten sein. Nebenwirkung: Text
    im Terminal markiert man jetzt mit ⇧+Ziehen (Shift schaltet die Maus am xterm.js
    vorbei zur Browser-Auswahl)."""
    for opt, val in (("status", "off"), ("mouse", "on")):
        _run([TMUX, "set-option", "-t", name, opt, val])


def kill_tmux(item_id: str) -> bool:
    """Die Session eines Items wirklich beenden (nicht Teil des Schließens —
    das Panel lässt sie absichtlich stehen)."""
    return _run([TMUX, "kill-session", "-t", tmux_name(item_id)]).returncode == 0


def holds_session(item_id: str) -> bool:
    """Hält das Terminal die Session dieses Items? Das ist die Besitzregel, die der
    Board-Runner vor jedem `--resume` fragt. Fehlt tmux oder ist die Id unbrauchbar,
    hält niemand etwas — ein kaputtes Terminal darf keinen Run blockieren."""
    if not _ITEM_RE.match(item_id or ""):
        return False
    try:
        return has_tmux(item_id)
    except (TerminalError, OSError, subprocess.SubprocessError):
        return False


def release(item_id: str) -> dict:
    """Session an das Board zurückgeben: tmux-Sitzung beenden (damit stirbt der
    interaktive Agent darin) und, falls der Betrachter gerade dieses Item zeigt,
    auch ihn. Das Transkript ist schon geschrieben — jede Session, headless wie
    interaktiv, schreibt dasselbe `~/.claude/projects/…/<uuid>.jsonl`; der nächste
    Board-Run resumt also mit allem, was im Terminal passiert ist."""
    killed = kill_tmux(item_id)
    rest = held_items()
    if not rest:
        # Nichts mehr zu zeigen: den Port freigeben statt einen leeren Betrachter zu halten.
        _stop_viewer(_read_state())
        _write_state({})
    return {"released": killed, "item": item_id, "held": rest, "open": bool(rest)}


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
    """Betrachter-Prozess beenden, tmux-Sessions bewusst stehen lassen. Die UI ruft das
    seit 2026-09-07 NICHT mehr beim Schließen eines Panels — der Betrachter ist geteilt;
    ein geschlossenes Iframe beendet nur seinen eigenen tmux-Client."""
    state = _read_state()
    _stop_viewer(state)
    _write_state({})
    return {"open": False}


def _ensure_viewer() -> dict:
    """Den einen ttyd starten, falls er nicht schon lebt. Er kennt kein Item — welche
    Sitzung er zeigt, sagt ihm jede Browser-Verbindung selbst (`?arg=gcterm-<item>`)."""
    state = _read_state()
    if _alive(state.get("ttyd_pid")) and state.get("port") == PORT:
        return {**state, "reused": True}
    _stop_viewer(state)                          # toter/alter Eintrag: aufräumen

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

    # `-W` = Tippen erlaubt, `-O` = WebSocket nur vom eigenen Origin (kein Drive-by
    # aus fremden Browser-Tabs), `-a` = die Sitzung kommt aus der URL. Der Attach ist
    # absichtlich NICHT `-r`.
    proc = subprocess.Popen(
        [TTYD, "-p", str(PORT), "-i", "127.0.0.1", "-W", "-O", "-a",
         "-t", "disableLeaveAlert=true", "-t", "titleFixed=gc-terminal",
         "sh", "-c", ATTACH_SCRIPT, "gc-attach"],
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

    new = {"ttyd_pid": proc.pid, "port": PORT}
    _write_state(new)
    return {**new, "reused": False}


def open_terminal(item_id: str, runner: str, session: str) -> dict:
    """tmux für das Item sicherstellen, den geteilten Betrachter sicherstellen, URL liefern."""
    for binary in (TMUX, TTYD):
        if not shutil.which(binary):
            raise TerminalError(f"{binary} ist nicht installiert (brew install {binary})")

    had_tmux = has_tmux(item_id)
    ensure_tmux(item_id, runner, session)
    viewer = _ensure_viewer()
    return {
        "item": item_id, "runner": runner, "session": session,
        "ttyd_pid": viewer["ttyd_pid"], "port": PORT,
        "url": url_for(item_id), "tmux": tmux_name(item_id),
        "open": True, "reused": had_tmux and viewer["reused"],
        "held": held_items(),
    }


def status() -> dict:
    """Was die UI wissen muss: läuft der Betrachter-Prozess (welche Items ein Terminal
    halten, sagt `held_items()` — das ist die tmux-Sicht, nicht der Betrachter)."""
    state = _read_state()
    if not _alive(state.get("ttyd_pid")):
        if state:
            _write_state({})
        return {"open": False}
    return {**state, "open": True}


def held_items() -> list[str]:
    """Alle Items, deren Session gerade ein Terminal hält (lebende `gcterm-*`).
    Fail-open wie `holds_session`: ohne tmux hält niemand etwas — das läuft im
    5-s-Poll der UI mit und darf das Board nicht rot färben."""
    try:
        res = _run([TMUX, "list-sessions", "-F", "#{session_name}"])
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    return [z[len("gcterm-"):] for z in res.stdout.split() if z.startswith("gcterm-")]


# ---------------------------------------------------------------- CLI


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("usage: terminal.py open <item-id> <claude|codex> <session-uuid>")
        print("       terminal.py close | status | held | kill <item-id> | release <item-id>")
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
        elif cmd == "release":
            out = release(rest[0])
        elif cmd == "held":
            out = {"held": held_items()}
        else:
            raise TerminalError(f"unbekannter Befehl: {cmd}")
    except (TerminalError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
