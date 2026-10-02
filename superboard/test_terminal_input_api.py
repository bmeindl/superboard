"""Board API contract for the independently observed terminal request state."""
import json
from urllib.request import urlopen

import terminal
from test_terminal import _post, _server  # noqa: F401 -- shared isolated HTTP fixture


def test_requests_reach_both_poll_and_reload(_server, monkeypatch):
    port, _ = _server
    state = {"aaaa11112222": {"state": "waiting", "request_id": "r1", "seen": False}}
    monkeypatch.setattr(terminal, "input_states", lambda: state)
    for route in ("/api/etag", "/api/board"):
        with urlopen(f"http://127.0.0.1:{port}{route}") as response:
            assert json.load(response)["terminal_input"] == state


def test_seen_forwards_exact_request_identity(_server, monkeypatch):
    port, _ = _server
    seen = []
    monkeypatch.setattr(terminal, "acknowledge", lambda *args: seen.append(args))
    monkeypatch.setattr(terminal, "input_states", lambda: {})
    assert _post(port, {"id": "aaaa11112222", "action": "seen", "request_id": "r1"})[0] == 200
    assert seen == [("aaaa11112222", "r1")]
    assert _post(port, {"id": "aaaa11112222", "action": "seen", "request_id": []})[0] == 400
    assert _post(port, {"id": "aaaa11112222", "action": "seen", "request_id": "x" * 257})[0] == 400
    assert len(seen) == 1


def test_observer_failure_does_not_reuse_stale_wait(_server, monkeypatch):
    port, _ = _server

    def broken():
        raise OSError("observer unavailable")

    monkeypatch.setattr(terminal, "input_states", broken)
    with urlopen(f"http://127.0.0.1:{port}/api/etag") as response:
        assert json.load(response)["terminal_input"] == {}
