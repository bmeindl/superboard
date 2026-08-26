"""Core/extension boundary tests for optional run receipts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import receipt_hook

HERE = Path(__file__).resolve().parent


def test_runner_import_and_git_context_work_without_receipt_module() -> None:
    code = r'''
import importlib.abc
import sys

class BlockReceipt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "receipt":
            raise ModuleNotFoundError("receipt intentionally absent", name="receipt")
        return None

sys.meta_path.insert(0, BlockReceipt())
import gc_runner
import receipt_hook
import server
from pathlib import Path
import tempfile

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    gc_runner.GIT_ANCHOR = root / "git-anchor.json"
    gc_runner.USAGE_LOG = root / "usage.jsonl"
    gc_runner.spawn_agent = lambda *_args, **_kwargs: {
        "ok": True, "reply": "done", "session_id": "", "denials": [],
        "usage_summary": {}, "context_tokens": 0, "runner": "claude",
        "raw_error": "", "killed": "",
    }
    posts = []
    gc_runner._post_append = lambda *args: posts.append(args)
    pending = {
        "addr": {"id": "abcabcabcabc", "name": "Dev", "col": "Jetzt"},
        "title": "No receipt", "body": [], "session": "", "gc_last": "",
        "thread": [{"kind": "ask", "text": "run"}], "last_ask": "run",
    }
    assert "## Git" in gc_runner._git_context("abcabcabcabc", False)
    out = gc_runner.run_item(
        pending, journal_dir=root / "journal", sidecar_dir=root / "threads",
    )
    assert out["ok"] and posts
    assert receipt_hook.write("abcabcabcabc", "No receipt", {}, {}, 0.0) is None
    assert receipt_hook.files("abcabcabcabc") == []
print("ok")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_broken_receipt_implementation_degrades_to_noop(monkeypatch, capsys) -> None:
    broken = ModuleType("receipt")

    def fail(*_args, **_kwargs):
        raise RuntimeError("telemetry exploded")

    broken.write = fail
    broken.RECEIPT_DIR = Path("/not-used")
    broken.receipt_files = fail
    monkeypatch.setattr(receipt_hook, "_IMPLEMENTATION", broken)
    assert receipt_hook.write("abcabcabcabc", "Broken", {}, {}, 0.0) is None
    assert receipt_hook.files("abcabcabcabc") == []
    assert "telemetry exploded" in capsys.readouterr().err
