"""First-run bootstrap owns mutable workspace files without clobbering users."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import date
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _cli_module():
    spec = importlib.util.spec_from_file_location("superboard_cli", HERE / "__main__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_creates_workspace_owned_instance_files(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    cli = _cli_module()

    board = cli._bootstrap(tmp_path)

    assert board == tmp_path / "inbox" / "board.md"
    assert board.is_file()
    assert (tmp_path / "inbox" / "gc-threads").is_dir()
    assert (tmp_path / ".superboard" / "journal").is_dir()
    seeded_actions = json.loads((tmp_path / "actions.json").read_text())["actions"]
    assert [a["key"] for a in seeded_actions] == ["superboard-update"]
    assert json.loads((tmp_path / "rituals.json").read_text())["rituals"] == {}
    config = json.loads((tmp_path / "board.config.json").read_text())
    assert config["owner"]["name"] == "You"
    assert config["night_pause"]["enabled"] is False
    skill = tmp_path / ".claude" / "skills" / "superboard" / "SKILL.md"
    assert skill.read_text(encoding="utf-8").startswith("---\nname: superboard\n")
    # The shipped cockpit card names this skill by path, so it must exist on day one.
    update_skill = tmp_path / ".claude" / "skills" / "superboard-update" / "SKILL.md"
    assert update_skill.read_text(encoding="utf-8").startswith("---\nname: superboard-update\n")


def test_starter_is_a_pending_setup_checklist() -> None:
    cli = _cli_module()
    ids = [f"{n:012x}" for n in range(len(cli.STARTER_ITEMS))]
    text = cli._starter_board(date(2026, 8, 22), ids)

    # Onboarding stays visibly separate from the empty home for normal work.
    assert re.findall(r"^## (.+)$", text, re.MULTILINE) == [
        "Getting started",
        "My to-dos",
    ]
    titles = re.findall(r"^- \[ \] (.+?) \*\(2026-08-22\)\*$", text, re.MULTILINE)
    assert titles == [
        "**1 · Start here · Meet Superboard**",
        "2 · Set up this workspace",
        "3 · Add your first real to-do",
        "4 · Understand runs, threads and cache",
        "5 · Find settings and get help",
        "6 · Check your agent and model setup",
        "7 · Set up your Cockpit",
        "8 · Set up an email digest",
        "9 · Set up one routine",
        "10 · Set up an off-duty view",
        "11 · Turn on night rest",
        "12 · Let Superboard learn from your threads",
        "13 · Get more from Superboard",
        "14 · Finish Getting started",
    ]
    # Core orientation and Cockpit payoff stay in Now; optional setup waits in Next.
    onboarding = text.split("## My to-dos", 1)[0]
    now = onboarding.split("### Next", 1)[0]
    next_ = onboarding.split("### Next", 1)[1].split("### Backlog", 1)[0]
    backlog = onboarding.split("### Backlog", 1)[1]
    assert now.count("\n- [ ] ") == 7 and next_.count("\n- [ ] ") == 6
    assert backlog.count("\n- [ ] ") == 1
    normal = text.split("## My to-dos", 1)[1].split("# To discuss", 1)[0]
    assert normal.count("\n- [ ] ") == 0
    assert "### Jetzt" not in text and "### Bald" not in text and "### Geparkt" not in text
    assert "# To discuss" in text and "# Notes" in text

    # Every card carries its own id and one pending user turn: nothing auto-runs,
    # and each step is startable on its own with ▶ Agent.
    assert text.count("@gc-id: ") == len(titles)
    assert all(f"@gc-id: {gc_id}" in text for gc_id in ids)
    assert text.count("@gc: ") == len(titles)
    # Each body splits into an overview line and the deep-dive mission.
    assert text.count("\n  ···\n") == len(titles)

    compact = " ".join(text.split())
    assert "I opened your Superboard introduction" in compact
    assert "one real current to-do" in compact
    assert "My to-dos" in compact
    assert "prompt cache is only an efficiency" in compact
    assert "There is no settings maze" in compact
    assert "narrow workspace" in compact
    assert "off_duty.hidden_topics" in compact
    assert "FIRST ensure exactly one follow-up card" in compact
    assert "platform and run profile" in compact
    assert "one real digest" in compact
    assert "rituals.json intentionally starts empty" in compact
    assert "night_pause" in compact and "enabled" in compact
    assert "archives the onboarding cards" in compact


def test_overlay_done_action_reuses_canonical_completion_path() -> None:
    """The onboarding cards can be completed where their work happens."""
    html = (HERE / "index.html").read_text(encoding="utf-8")

    assert 'const completeBtn = btn("✓ Done"' in html
    assert "await toggleDone(loc, it, true)" in html
    assert "closeGcOverlay();" in html
    assert "it stays visible until reload so you can undo" in html


def test_final_onboarding_card_closes_only_after_the_normal_done_path() -> None:
    html = (HERE / "index.html").read_text(encoding="utf-8")

    assert 'it.title === "Finish Getting started"' in html
    assert 'await gcAppend(loc, it, "done", "")' in html
    assert "if (closeStarter) await closeOnboarding(it.id)" in html
    assert 'fetch("/api/onboarding-close"' in html
    assert "Finish or consciously skip first" in html


def test_workspace_path_is_explicit_or_falls_back_to_cwd(tmp_path: Path, monkeypatch) -> None:
    cli = _cli_module()
    monkeypatch.delenv("GC_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    assert cli._workspace_from_args("~/Example")[1] is True
    assert cli._workspace_from_args(None) == (tmp_path.resolve(), False)

    monkeypatch.setenv("GC_ROOT", str(tmp_path / "configured"))
    assert cli._workspace_from_args(None) == ((tmp_path / "configured").resolve(), True)


def test_claude_preflight_is_non_fatal_and_truthful(tmp_path: Path, monkeypatch) -> None:
    cli = _cli_module()
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli._claude_status(tmp_path)[0] == "missing"

    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/claude")

    class Result:
        returncode = 0
        stdout = '{"loggedIn": true}'

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Result())
    assert cli._claude_status(tmp_path)[0] == "ready"


def test_repo_boundary_finds_exact_or_containing_repository(tmp_path: Path) -> None:
    cli = _cli_module()
    repo = tmp_path / "project"
    nested = repo / "packages" / "app"
    (repo / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)

    assert cli._repo_boundary(repo) == repo
    assert cli._repo_boundary(nested) == repo
    assert cli._repo_boundary(tmp_path / "elsewhere") is None


def test_starter_ids_are_unique_without_explicit_ids() -> None:
    cli = _cli_module()
    ids = re.findall(r"@gc-id: (\S+)", cli._starter_board(date(2026, 8, 22)))
    assert len(ids) == len(cli.STARTER_ITEMS) == len(set(ids))


def test_bootstrap_never_overwrites_existing_workspace_files(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    cli = _cli_module()
    existing = {
        "actions.json": '{"mine": "actions"}\n',
        "rituals.json": '{"mine": "rituals"}\n',
        "board.config.json": '{"mine": "config"}\n',
        ".claude/skills/superboard/SKILL.md": "mine\n",
    }
    for relative, content in existing.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    cli._bootstrap(tmp_path)
    cli._bootstrap(tmp_path)

    for relative, content in existing.items():
        assert (tmp_path / relative).read_text(encoding="utf-8") == content


def test_bootstrap_migrates_legacy_ritual_file_before_seeding_empty_default(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    legacy = '{"active_from": "", "rituale": {"daily": {"title": "Keep me"}}}\n'
    (tmp_path / "rituale.json").write_text(legacy, encoding="utf-8")

    _cli_module()._bootstrap(tmp_path)

    assert (tmp_path / "rituals.json").read_text(encoding="utf-8") == legacy


def test_board_client_path_does_not_move_with_runtime_data(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "workspace"
    data = tmp_path / "runtime-elsewhere"
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.setenv("GC_DATA", str(data))

    _cli_module()._bootstrap(root)

    assert (root / ".superboard" / "board_write.py").is_file()
    assert not (data / "board_write.py").exists()
    assert (data / "journal").is_dir()


def test_fresh_workspace_never_auto_spends_tokens(tmp_path: Path, monkeypatch) -> None:
    """A first install must not fire the scheduled triage run on its own.

    Found in the built-wheel rig on 2026-08-22: starting a freshly installed board
    immediately spawned an Opus triage run although nobody had clicked anything —
    exactly what the README and the architecture invariant rule out.
    """
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    cli = _cli_module()
    board = cli._bootstrap(tmp_path)

    import server

    assert server._workspace_ever_ran_an_agent(board) is False
    (board.parent / "gc-threads" / "abc123-20260822-190000-0000.md").write_text("x")
    assert server._workspace_ever_ran_an_agent(board) is True
