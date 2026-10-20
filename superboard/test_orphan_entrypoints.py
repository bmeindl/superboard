"""A leftover agent from before a board restart blocks every writer on its session:
not only ▶ runs, but also the terminal and /compact. Plus: `sub` needs `done`."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import gc_runner
import server

GID = "0abcdef00002"
BOARD = f"""## Dev

### Jetzt

- [ ] Orphaned card *(2026-10-20)*
  @gc-id: {GID}
  @gc: question
  @gc-session: 9988fde2-624f-4c76-aa00-209113c27cdd · orphaned-card
  - [ ] Step

### Bald

### Geparkt

# Personen

# Notizen
"""


def _post(url: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_orphan_blocks_terminal_and_compact_and_sub_needs_done(tmp_path, monkeypatch):
    board = tmp_path / "board.md"
    board.write_text(BOARD)
    jd = tmp_path / "journal"
    jd.mkdir()
    monkeypatch.setattr(gc_runner, "JOURNAL_DIR", jd)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    (jd / f"run-{GID}-1.meta.json").write_text(json.dumps(
        {"gc_id": GID, "title": "T", "status": "running", "pid": proc.pid,
         "started": time.time() - 60, "timeout": 900}))
    server.Handler.board_path = board
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for path in ("/api/gc-compact", "/api/gc-terminal"):
            code, body = _post(url, path, {"id": GID})
            assert code == 409 and body["error"] == server.ORPHAN_RUN_MSG, (path, code, body)
        assert GID not in server.RUNNING and GID not in server.COMPACTING

        before = board.read_text()
        code, body = _post(url, "/api/gc-body", {"addr": {"id": GID}, "stage": "tested", "sub": 1})
        assert code == 400 and "sub needs done" in body["error"]
        assert board.read_text() == before
    finally:
        httpd.shutdown()
        proc.kill()
