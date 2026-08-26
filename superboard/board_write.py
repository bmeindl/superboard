#!/usr/bin/env python3
"""The board client: everything an agent is allowed to write, in one file.

Superboard has a single-writer invariant — only the running server writes
``inbox/board.md``. Agents therefore never edit that file; they call this client,
which talks to the local board server over HTTP. This file is deliberately
**pure standard library and self-contained**: it is copied into every workspace at
``.superboard/board_write.py`` so that it works from any ``python3``, regardless of
how the server itself was installed (uvx, pipx, a venv, a source checkout). A
client that only works when the package happens to be importable is a client the
agent will quietly route around — and routing around it means hand-editing
``board.md``, which is exactly the invariant this exists to protect.

Verbs:

    --show          print the current body and its bodyEtag (no write)
    --body-file     replace this item's body (needs --body-etag)
    --stage         append a process stage to this item
    --new-card      create a to-do (never starts a run)
    --ensure-card   create a to-do only when that exact title is not already active
    --new-topic     create an empty board topic (row)
    --docs          print the product's own README / architecture doc

Examples:

    python3 .superboard/board_write.py --id a1b2c3d4e5f6 --show
    python3 .superboard/board_write.py --id a1b2c3d4e5f6 \
      --body-file /tmp/body.md --body-etag 0123456789abcdef
    python3 .superboard/board_write.py --id a1b2c3d4e5f6 \
      --stage 'tested · pytest *(2026-08-22)*'
    python3 .superboard/board_write.py --new-card 'Renew the domain' \
      --topic Admin --col Jetzt --card-body-file /tmp/notes.md
    python3 .superboard/board_write.py --new-topic 'Clients'
    python3 .superboard/board_write.py --docs readme
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:47822"
COLUMNS = ("Jetzt", "Bald", "Geparkt")


def item_body_etag(body: list[str]) -> str:
    """Revision of an item body for optimistic, item-local writes.

    Kept byte-identical to ``server.item_body_etag`` on purpose: this client must
    not import the package (see the module docstring), so the one hash both sides
    agree on is duplicated rather than shared. ``test_board_write.py`` asserts the
    two implementations still produce the same value.
    """
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _items(board: dict):
    for theme in board.get("themes", []):
        for items in theme.get("cols", {}).values():
            yield from items
    yield from board.get("staging", [])
    yield from board.get("cockpit", [])
    for person in board.get("persons", []):
        yield from person.get("items", [])


def get(base_url: str, path: str, *, raw: bool = False):
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        if raw:
            return response.read().decode("utf-8")
        return json.load(response)


def fetch_board(base_url: str) -> dict:
    return get(base_url, "/api/board")


def fetch_item(base_url: str, gc_id: str) -> dict:
    payload = fetch_board(base_url)
    matches = [it for it in _items(payload.get("board", {})) if it.get("id") == gc_id]
    if len(matches) != 1:
        raise ValueError(f"item not uniquely found: {gc_id} ({len(matches)} matches)")
    return matches[0]


def post(base_url: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = {"error": exc.reason}
        raise RuntimeError(f"HTTP {exc.code}: {detail.get('error', detail)}") from exc


def _text_from(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def new_topic(base_url: str, name: str) -> dict:
    """Add an empty topic through the whole-board endpoint, under its ETag.

    There is no narrower endpoint: topics are rows of the board file, and the
    server owns serialization. Read-modify-write under ``baseEtag`` keeps the
    single-writer invariant intact — a conflicting write loses loudly (409)
    instead of silently clobbering the owner's board.
    """
    name = name.strip()
    if not name:
        raise ValueError("topic name is empty")
    payload = fetch_board(base_url)
    board = payload.get("board", {})
    existing = {th.get("name", "").strip().lower() for th in board.get("themes", [])}
    if name.lower() in existing:
        return {"ok": True, "changed": False, "topic": name, "note": "topic already exists"}
    board.setdefault("themes", []).append({"name": name, "cols": {c: [] for c in COLUMNS}})
    result = post(base_url, "/api/board", {"board": board, "baseEtag": payload.get("etag")})
    return {"ok": True, "changed": True, "topic": name, "etag": result.get("etag")}


def new_card(base_url: str, title: str, topic: str, col: str, body: list[str], ask: str) -> dict:
    """Create a to-do without starting a run.

    ``run=false`` is not optional here: an agent creating cards on the user's
    behalf must not also spend the user's tokens on them. The user presses
    ▶ Agent when they want the work to happen.
    """
    payload: dict = {
        "text": ask or title,
        "title": title,
        "col": col,
        "body": body,
        "run": False,
        "model": "",
    }
    if topic:
        payload["theme"] = topic
    return post(base_url, "/api/quick-capture", payload)


def ensure_card(base_url: str, title: str, topic: str, col: str,
                body: list[str], ask: str) -> dict:
    """Idempotently create one active card with an exact title."""
    wanted = title.strip().casefold()
    matches = [it for it in _items(fetch_board(base_url).get("board", {}))
               if str(it.get("title", "")).strip().casefold() == wanted]
    if matches:
        return {"ok": True, "changed": False, "id": matches[0].get("id"),
                "title": title, "note": "card already exists"}
    result = new_card(base_url, title, topic, col, body, ask)
    return {**result, "changed": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--id", help="immutable @gc-id of the target item")
    parser.add_argument("--url", default=os.environ.get("GC_BOARD_URL", DEFAULT_URL),
                        help="board server base URL (default: %(default)s)")
    parser.add_argument("--show", action="store_true",
                        help="print the current body and bodyEtag; do not write")
    parser.add_argument("--body-file", metavar="PATH",
                        help="replacement body for --id; use - for stdin")
    parser.add_argument("--body-etag",
                        help="bodyEtag from the run prompt or --show (required with --body-file)")
    parser.add_argument("--stage",
                        help="single-line text after @stage:, e.g. 'tested · pytest *(2026-08-22)*'")
    parser.add_argument("--new-card", metavar="TITLE", help="create a to-do with this title")
    parser.add_argument("--ensure-card", metavar="TITLE",
                        help="create a to-do unless that exact title is already active")
    parser.add_argument("--topic", default="", help="target topic for --new-card")
    parser.add_argument("--col", default="Jetzt", choices=COLUMNS,
                        help="target column for --new-card (default: %(default)s)")
    parser.add_argument("--card-body-file", metavar="PATH",
                        help="body lines for --new-card; use - for stdin")
    parser.add_argument("--ask", default="",
                        help="first queued @gc: turn for --new-card (defaults to the title)")
    parser.add_argument("--new-topic", metavar="NAME", help="create an empty board topic")
    parser.add_argument("--docs", choices=("readme", "architecture", "changelog"),
                        help="print the product's own documentation and exit")
    args = parser.parse_args(argv)
    base_url = args.url.rstrip("/")

    try:
        if args.docs:
            print(get(base_url, f"/api/docs/{args.docs}", raw=True))
            return 0

        if args.new_topic:
            print(json.dumps(new_topic(base_url, args.new_topic), ensure_ascii=False))
            return 0

        if args.new_card or args.ensure_card:
            body = _text_from(args.card_body_file).splitlines() if args.card_body_file else []
            title = args.new_card or args.ensure_card
            result = (ensure_card(base_url, title, args.topic, args.col, body, args.ask)
                      if args.ensure_card else
                      new_card(base_url, title, args.topic, args.col, body, args.ask))
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if not args.id:
            parser.error("--id is required for --show, --body-file, and --stage")

        if args.show:
            if args.body_file is not None or args.stage is not None:
                parser.error("--show cannot be combined with write options")
            item = fetch_item(base_url, args.id)
            print(json.dumps({"id": args.id, "body": item.get("body", []),
                              "bodyEtag": item_body_etag(item.get("body", []))},
                             ensure_ascii=False, indent=2))
            return 0

        if args.body_file is None and args.stage is None:
            parser.error("one of --show, --body-file, --stage, --new-card, --new-topic, "
                         "or --docs is required")
        if args.body_file is not None and not args.body_etag:
            parser.error("--body-etag is required with --body-file")

        payload: dict = {"addr": {"id": args.id}}
        if args.body_file is not None:
            payload.update({"body": _text_from(args.body_file), "bodyEtag": args.body_etag})
        if args.stage is not None:
            payload["stage"] = args.stage
        print(json.dumps(post(base_url, "/api/gc-body", payload), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"board_write: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
