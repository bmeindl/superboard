"""The opt-in starter supports fresh installs and existing, user-owned workspaces."""
import json
import sys

import pytest

from superboard import __main__ as cli
from superboard.crew_setup import install_crew


def test_setup_cli_preserves_existing_board_and_customized_crew(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "release desk"
    for key in ("GC_ROOT", "GC_BOARD", "GC_DATA"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(sys, "argv", ["superboard", "--setup-crew", str(workspace)])
    monkeypatch.setattr(cli, "_print_preflight", lambda *args, **kwargs: None)
    cli.run()
    assert "@writer " in capsys.readouterr().out
    board = workspace / "inbox/board.md"
    board.write_text("# My earlier solo cards\n")
    crew = workspace / "crew"
    manifest = json.loads((crew / "crew.json").read_text())
    for role in manifest["roles"]:
        assert (crew / "roles" / role / "ROLE.md").is_file()
    for path in ("README.md", "blueprints/ROLE.md", "examples/release-1.md", "examples/release-2.md",
                 "rituals/retrospective.md", "rituals/preferences.md", "rituals/effect-check.md"):
        assert (crew / path).stat().st_size > 0
    writer = crew / "roles/writer/ROLE.md"
    writer.write_text("My own writing method")
    cli.run()
    assert "created 0 files" in capsys.readouterr().out
    assert board.read_text() == "# My earlier solo cards\n"
    assert writer.read_text() == "My own writing method"


def test_solo_bootstrap_stays_solo(tmp_path, monkeypatch):
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    cli._bootstrap(tmp_path)
    assert not (tmp_path / "crew").exists()


def test_setup_refuses_symlink_before_installing_manifest(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    roles = tmp_path / "crew/roles"
    roles.mkdir(parents=True)
    (roles / "writer").symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError, match="symlink"):
        install_crew(tmp_path)
    assert not (tmp_path / "crew/crew.json").exists()
    assert not list(other.iterdir())
