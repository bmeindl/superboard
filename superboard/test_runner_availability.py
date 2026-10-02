"""An unavailable default provider must not disable another authenticated runner."""
import subprocess

import gc_runner
import server


def test_codex_status_uses_its_own_cli_and_never_returns_cli_output(tmp_path, monkeypatch):
    cli = tmp_path / 'codex-test'
    cli.write_text('#!/bin/sh\nprintf "private login detail"\nexit 0\n')
    cli.chmod(0o700)
    monkeypatch.setenv('GC_RUNNER_CODEX', str(cli))
    monkeypatch.setattr(server, 'runner_status', lambda: ('login', 'Claude needs login'))
    state, message = server.codex_runner_status()
    assert state == 'ready'
    assert 'Codex' in message and 'private login detail' not in message
    cli.write_text('#!/bin/sh\nexit 1\n')
    assert server.codex_runner_status()[0] == 'login'


def test_missing_and_unverifiable_codex_are_distinct(tmp_path, monkeypatch):
    cli = tmp_path / 'missing-codex'
    monkeypatch.setenv('GC_RUNNER_CODEX', str(cli))
    assert server.codex_runner_status()[0] == 'missing'
    cli.write_text('placeholder')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('codex', 5)
    monkeypatch.setattr(server.subprocess, 'run', timeout)
    assert server.codex_runner_status()[0] == 'unknown'


def test_codex_cmd_follows_moved_app_binary(tmp_path, monkeypatch):
    """ChatGPT 26.928 moved the binary; resolution happens per call, newest location first."""
    new, old = tmp_path / 'new' / 'codex', tmp_path / 'old' / 'codex'
    monkeypatch.delenv('GC_RUNNER_CODEX', raising=False)
    monkeypatch.setattr(gc_runner, '_CODEX_CANDIDATES', (str(new), str(old)))
    monkeypatch.setattr(gc_runner.shutil, 'which', lambda name: None)
    assert gc_runner.codex_cmd() == str(new)  # nothing installed: newest expected location
    old.parent.mkdir()
    old.write_text('#!/bin/sh\n')
    old.chmod(0o700)
    assert gc_runner.codex_cmd() == str(old)
    new.parent.mkdir()
    new.write_text('#!/bin/sh\n')
    new.chmod(0o700)
    assert gc_runner.codex_cmd() == str(new)
    monkeypatch.setenv('GC_RUNNER_CODEX', '/custom/codex')
    assert gc_runner.codex_cmd() == '/custom/codex'
