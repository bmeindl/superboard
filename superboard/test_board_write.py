"""CLI regression for board_write.py — a real HTTP server, temp board only."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import board_write
import server


BOARD = """## Dev

### Jetzt

- [ ] Build the endpoint *(2026-08-22)*
  Old body
  @gc-id: a1b2c3d4e5f6

### Bald

### Geparkt

# Personen

# Notizen
"""


def _serve(path):
    server.Handler.board_path = path
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_show_body_replace_stage_and_conflict(tmp_path, capsys):
    board = tmp_path / "board.md"
    body_file = tmp_path / "body.md"
    board.write_text(BOARD)
    body_file.write_text("New note\n\n···\n### Working state\nStatus: tested\n")
    httpd, url = _serve(board)
    try:
        assert board_write.main(["--url", url, "--id", "a1b2c3d4e5f6", "--show"]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown == {
            "id": "a1b2c3d4e5f6",
            "body": ["Old body"],
            "bodyEtag": server.item_body_etag(["Old body"]),
        }

        assert board_write.main([
            "--url", url, "--id", "a1b2c3d4e5f6",
            "--body-file", str(body_file), "--body-etag", shown["bodyEtag"],
            "--stage", "tested · pytest *(2026-08-22)*",
        ]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is True and result["changed"] is True
        item = server.find_item(server.parse_board(board.read_text()), {"id": "a1b2c3d4e5f6"})[0]
        assert item["body"] == ["New note", "···", "### Working state", "Status: tested"]
        assert [stage["stage"] for stage in item["stages"]] == ["tested"]

        # The old --show snapshot is now stale: the CLI surfaces the 409 and returns
        # nonzero instead of overwriting the newer body.
        assert board_write.main([
            "--url", url, "--id", "a1b2c3d4e5f6",
            "--body-file", str(body_file), "--body-etag", shown["bodyEtag"],
        ]) == 1
        assert "HTTP 409" in capsys.readouterr().err
    finally:
        httpd.shutdown()


def test_ensure_card_is_idempotent(tmp_path, capsys):
    board = tmp_path / "board.md"
    board.write_text(BOARD)
    httpd, url = _serve(board)
    args = ["--url", url, "--ensure-card", "Cockpit extension · Add a useful recurring action",
            "--topic", "Dev", "--col", "Jetzt"]
    try:
        assert board_write.main(args) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["changed"] is True
        assert board_write.main(args) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["changed"] is False
        parsed = server.parse_board(board.read_text())
        titles = [it["title"] for theme in parsed["themes"]
                  for col in theme["cols"].values() for it in col]
        assert titles.count("Cockpit extension · Add a useful recurring action") == 1
    finally:
        httpd.shutdown()
