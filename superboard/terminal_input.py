"""Observe Codex's real approval protocol without answering or parsing terminal text.

The interactive TUI connects through a private Unix WebSocket to an app-server
owned by this terminal. Messages pass unchanged; only request lifecycle metadata
is retained. This process and its state outlive the Board server and browser.
Claude, OpenCode, and terminals opened before this observer report unknown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path


REQUESTS = {
    "item/commandExecution/requestApproval": "Permission required",
    "item/fileChange/requestApproval": "Permission required",
    "item/permissions/requestApproval": "Permission required",
    "item/tool/requestUserInput": "Question needs an answer",
    "mcpServer/elicitation/request": "Input required",
}
FALLBACK = 75


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RequestState:
    """Small protocol reducer; a keystroke or client response is not resolution."""

    def __init__(self, path: Path, session: str, launch: str):
        self.path = path
        self.session = session
        self.launch = launch
        self.pending: dict[str, dict] = {}
        self.state = "unknown"
        self.reason = "Connecting to terminal"
        self.publish()

    def key(self, request_id: str | int) -> str:
        return self.launch + ":" + json.dumps(request_id, separators=(",", ":"))

    def publish(self) -> None:
        # The per-item launch file is the ownership pointer. An older observer
        # may finish late, but it must not overwrite the newer launch's state.
        owner = read_json(self.path.with_name(self.path.stem + ".launch.json"))
        if owner.get("launch") and owner["launch"] != self.launch:
            return
        first = next(iter(self.pending.values()), None)
        snapshot = {
            "state": "waiting" if first and self.state == "ready" else self.state,
            "request_id": first["request_id"] if first else None,
            "reason": first["reason"] if first and self.state == "ready" else self.reason,
            "since": first["since"] if first else None,
            "runner": "codex", "session": self.session, "launch": self.launch,
            "bridge_pid": os.getpid(), "updated": time.time(),
        }
        try:
            write_json(self.path, snapshot)
        except OSError:
            # Monitoring must never prevent the human from answering. The Board
            # marks snapshots without a fresh heartbeat unknown after 15 seconds.
            pass

    def observe(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return
        # Resume/start responses and notifications prove the target thread is loaded.
        thread = (message.get("result") or {}).get("thread") if isinstance(message.get("result"), dict) else None
        if method == "thread/started":
            thread = params.get("thread")
        if isinstance(thread, dict) and thread.get("id") == self.session:
            self.state, self.reason = "ready", "No confirmed input request"
            self.publish()
        if params.get("threadId") != self.session:
            return
        if method in REQUESTS and isinstance(message.get("id"), (str, int)):
            key = self.key(message["id"])
            self.pending.setdefault(key, {"request_id": key, "reason": REQUESTS[method], "since": time.time()})
            self.state, self.reason = "ready", "No confirmed input request"
        elif method == "serverRequest/resolved":
            self.pending.pop(self.key(params.get("requestId")), None)
        elif method in {"turn/completed", "thread/closed", "thread/archived"}:
            self.pending.clear()
        else:
            return
        self.publish()

    def unavailable(self, reason: str, *, ended: bool = False) -> None:
        self.pending.clear()
        self.state, self.reason = ("ended" if ended else "unknown"), reason
        self.publish()


async def observer_failed(done: set, tui_wait, server_wait, disconnect_wait,
                          grace_seconds: float = 0.4) -> bool:
    """Separate observer failure from the socket-first edge of a normal TUI quit."""
    if tui_wait in done:
        return False
    if server_wait in done:
        return True
    if disconnect_wait not in done:
        return False
    grace, _ = await asyncio.wait(
        [tui_wait, server_wait], timeout=grace_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    return server_wait in grace or tui_wait not in grace


async def run(codex: str, session: str, path: Path, launch: str) -> int:
    # Imported only by the terminal helper. The Board can still report unknown
    # for a legacy terminal without this optional runtime dependency installed.
    from websockets.asyncio.server import unix_serve

    state = RequestState(path, session, launch)
    server = None
    tui = None
    heartbeat = None
    diagnostics = tempfile.TemporaryFile()
    stop = asyncio.Event()
    disconnected = asyncio.Event()
    fallback = False
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async def keep_fresh():
            while True:
                await asyncio.sleep(2)
                state.publish()

        heartbeat = asyncio.create_task(keep_fresh())
        # /private/tmp avoids macOS's long user tempdir exceeding SUN_LEN.
        with tempfile.TemporaryDirectory(prefix="gc-input-", dir="/private/tmp") as directory:
            socket_path = str(Path(directory) / "tui.sock")
            server = await asyncio.create_subprocess_exec(
                codex, "app-server", "--stdio", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=diagnostics,
                limit=16 * 1024 * 1024,
            )
            connected = False

            async def connection(ws):
                nonlocal connected
                if connected:
                    await ws.close(code=1008, reason="Terminal already connected")
                    return
                connected = True

                async def to_server():
                    async for message in ws:
                        # TUI sends JSON text. Pass it through without adding,
                        # answering, or changing any approval/configuration.
                        if not isinstance(message, str):
                            raise ValueError("Unexpected binary Codex message")
                        server.stdin.write(message.encode() + b"\n")
                        await server.stdin.drain()

                async def to_tui():
                    while raw := await server.stdout.readline():
                        message = raw.decode()
                        try:
                            value = json.loads(message)
                            if isinstance(value, dict):
                                state.observe(value)
                        except (ValueError, TypeError):
                            # Never hide actual server output from the terminal.
                            pass
                        await ws.send(message.rstrip("\n"))

                tasks = [asyncio.create_task(to_server()), asyncio.create_task(to_tui())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    state.unavailable("Terminal connection lost")
                    disconnected.set()

            async with unix_serve(connection, path=socket_path, compression=None, max_size=16 * 1024 * 1024):
                tui = await asyncio.create_subprocess_exec(codex, "--remote", "unix://" + socket_path, "resume", session)
                tui_wait = asyncio.create_task(tui.wait())
                server_wait = asyncio.create_task(server.wait())
                stop_wait = asyncio.create_task(stop.wait())
                disconnect_wait = asyncio.create_task(disconnected.wait())
                waiters = [tui_wait, server_wait, stop_wait, disconnect_wait]
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                # The observer is an optional sidecar. If its backend or transport
                # fails while the terminal is still active, replace this process
                # with a plain `codex resume` after cleanup instead of closing the
                # human's terminal. A deliberate TUI exit remains a normal exit.
                # A normal interactive quit closes the socket a fraction before
                # the TUI process exits. Give that exit a brief chance to land;
                # otherwise a deliberate quit would reopen the terminal.
                fallback = await observer_failed(done, tui_wait, server_wait, disconnect_wait)
                for task in waiters:
                    task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        for process in (tui, server):
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        state.unavailable("Input observer unavailable" if fallback else "Terminal session ended",
                          ended=not fallback)
        if server is not None and server.returncode not in (None, 0, -signal.SIGTERM):
            diagnostics.seek(0, os.SEEK_END)
            diagnostics.seek(max(0, diagnostics.tell() - 4096))
            details = diagnostics.read().decode(errors="replace").strip()
            print(f"[gc] Codex backend exited ({server.returncode}). {details}", file=sys.stderr)
        diagnostics.close()
    return FALLBACK if fallback else (tui.returncode if tui and tui.returncode is not None else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--launch", default=None)
    args = parser.parse_args()
    try:
        launch = args.launch or uuid.uuid4().hex
        result = asyncio.run(run(args.codex, args.session, args.state, launch))
        if result == FALLBACK:
            os.execv(args.codex, [args.codex, "resume", args.session])
        return result
    except Exception as exc:
        print(f"[gc] input observer could not start: {exc}", file=sys.stderr)
        # Detection must never take the terminal away. Startup/import failures
        # degrade to the exact direct resume command used without the observer.
        try:
            write_json(args.state, {"state": "unknown", "runner": "codex",
                       "session": args.session, "launch": args.launch,
                       "reason": "Input observer unavailable", "updated": time.time()})
            os.execv(args.codex, [args.codex, "resume", args.session])
        except OSError as fallback_exc:
            print(f"[gc] direct Codex resume also failed: {fallback_exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
