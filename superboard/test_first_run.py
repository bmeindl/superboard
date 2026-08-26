"""Regression cover for the three verified first-run breaks (2026-08-24).

A fresh install had a newcomer path that was not true:

1. the very first card sent the agent to `README.md`/`ARCHITEKTUR.md`, which a
   fresh workspace never receives;
2. cards asked the agent to create topics and to-dos, but the workspace carried
   no client for that and the bundled skill named no write mechanism — an agent
   in that position hand-edits `board.md` and breaks the single-writer invariant;
3. "Claude Code is not installed" was printed to the terminal only, so the
   browser offered a ▶ Agent button that silently did nothing.

Each test below fails if one of those regresses.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _cli_module():
    spec = importlib.util.spec_from_file_location("superboard_cli_first_run", HERE / "__main__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _board_write_module():
    spec = importlib.util.spec_from_file_location("superboard_board_write", HERE / "board_write.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Break 1: the first card must not read files the workspace does not have ──

def test_starter_missions_never_point_at_unseeded_workspace_docs() -> None:
    cli = _cli_module()
    seeded = set(cli.WORKSPACE_STARTER_FILES) | {"inbox/board.md", ".superboard/board_write.py"}
    assert "README.md" not in seeded and "ARCHITEKTUR.md" not in seeded  # premise of this test
    for _col, title, short, mission, ask in cli.STARTER_ITEMS:
        text = " ".join((short, mission, ask))
        assert "ARCHITEKTUR.md" not in text, (
            f"starter card {title!r} points the agent at ARCHITEKTUR.md, which a fresh "
            "workspace never receives — use `board_write.py --docs architecture` instead"
        )
        # `context/README.md` is different: that is a file a setup card OFFERS TO CREATE
        # in the user's workspace, not a product doc it expects to already be there.
        for hit in re.finditer(r"(\S*)README\.md", text):
            assert hit.group(1).endswith("/"), (
                f"starter card {title!r} reads a bare README.md, which a fresh workspace "
                "never receives — use `board_write.py --docs readme` instead"
            )


def test_first_card_is_an_agent_led_introduction_not_a_readme_round() -> None:
    cli = _cli_module()
    _col, title, short, mission, _ask = cli.STARTER_ITEMS[0]
    assert "Start here" in title and "Meet Superboard" in title
    assert "Press ▶ Agent once" in short
    assert "GC_BOARD_URL" in mission and "/onboarding-showcase" in mission
    assert "open` on macOS" in mission and "xdg-open" in mission
    assert "--docs readme" not in mission


def test_orientation_has_no_hard_coded_card_specific_cta() -> None:
    source = (HERE / "index.html").read_text(encoding="utf-8")
    assert "onboarding-tour" not in source
    assert "Open desktop tour ↗" not in source
    assert "Open thread & cache guide ↗" not in source


def test_product_docs_are_served_rather_than_copied() -> None:
    sys.path.insert(0, str(HERE))
    import server

    assert (server.read_product_doc("readme") or "").strip(), "README must be resolvable"
    assert (server.read_product_doc("architecture") or "").strip()
    assert server.read_product_doc("nonsense") is None


# ── Break 2: agents need a write path that actually exists and actually runs ──

def test_bootstrap_seeds_the_board_client(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    cli = _cli_module()
    cli._bootstrap(tmp_path)
    client = tmp_path / ".superboard" / "board_write.py"
    assert client.is_file()
    assert client.read_bytes() == (HERE / "board_write.py").read_bytes()


def test_board_client_is_refreshed_on_every_start(tmp_path: Path, monkeypatch) -> None:
    """Unlike the user's own files it is product mechanics — an upgrade must not
    leave an agent holding an older client."""
    monkeypatch.delenv("GC_BOARD", raising=False)
    monkeypatch.delenv("GC_DATA", raising=False)
    cli = _cli_module()
    cli._bootstrap(tmp_path)
    client = tmp_path / ".superboard" / "board_write.py"
    client.write_text("# stale\n", encoding="utf-8")
    cli._bootstrap(tmp_path)
    assert client.read_bytes() == (HERE / "board_write.py").read_bytes()


def test_board_client_runs_without_the_package_importable(tmp_path: Path) -> None:
    """The whole point of the workspace copy: the agent's shell is a separate
    process that may not be able to `import superboard` at all."""
    client = tmp_path / "board_write.py"
    client.write_bytes((HERE / "board_write.py").read_bytes())
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-I", str(client), "--help"],
        capture_output=True, text=True, cwd=tmp_path, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    for verb in ("--show", "--body-file", "--stage", "--new-card", "--new-topic", "--docs"):
        assert verb in proc.stdout


def test_client_and_server_agree_on_the_body_revision() -> None:
    sys.path.insert(0, str(HERE))
    import server

    client = _board_write_module()
    for body in ([], ["one"], ["a", "···", "b — ü"], ["x" * 500]):
        assert client.item_body_etag(body) == server.item_body_etag(body)


def test_the_seeded_skill_documents_the_write_path() -> None:
    skill = (HERE / "superboard-skill.md").read_text(encoding="utf-8")
    assert ".superboard/board_write.py" in skill
    assert "--new-card" in skill and "--new-topic" in skill
    assert "only writer" in skill.lower() or "single-writer" in skill.lower()


def test_the_run_contract_names_the_workspace_client_not_a_module() -> None:
    sys.path.insert(0, str(HERE))
    import contract
    import gc_runner

    full = contract.render("full")
    reminder = contract.render("reminder")
    for rendered in (full, reminder):
        assert ".superboard/board_write.py" in rendered
        assert "-m superboard.board_write" not in rendered
    assert str(gc_runner.BOARD_WRITE).endswith("board_write.py")


def test_cards_that_create_things_name_the_client() -> None:
    cli = _cli_module()
    missions = {title: mission for _c, title, _s, mission, _a in cli.STARTER_ITEMS}
    real_work = next(m for t, m in missions.items() if "Add your first real to-do" in t)
    assert "--new-card" in real_work and "--topic 'My to-dos'" in real_work
    assert "--new-topic" not in real_work
    assert "never edit inbox/board.md" in real_work.lower()


# ── Break 3: the runner check has to reach the browser ───────────────────────

def test_runner_status_has_one_implementation() -> None:
    sys.path.insert(0, str(HERE))
    import server

    cli = _cli_module()
    state, message = server.runner_status(Path.cwd())
    assert state in {"missing", "login", "ready", "unknown"}
    assert message
    assert cli._claude_status(Path.cwd()) == (state, message)


def test_runner_status_is_exposed_and_rendered() -> None:
    source = (HERE / "server.py").read_text(encoding="utf-8")
    assert '"/api/runner-status"' in source
    index = (HERE / "index.html").read_text(encoding="utf-8")
    assert "/api/runner-status" in index
    assert "RUNNER_HELP" in index
    assert "not installed" in index and "not signed in" in index


# ── Checklist shape: finite, and the payoff arrives early ────────────────────

def test_the_first_real_hand_off_follows_the_introduction() -> None:
    cli = _cli_module()
    titles = [title for _c, title, _s, _m, _a in cli.STARTER_ITEMS]
    assert 8 <= len(titles) <= 13, "the checklist stays finite without bundling setup"
    assert "Start here" in titles[0]
    assert "Set up this workspace" in titles[1]
    assert "Add your first real to-do" in titles[2]
    assert not any("Find your way around" in title for title in titles)


def test_setup_concerns_are_concrete_and_cockpit_is_in_now() -> None:
    titles = [title.strip("*") for _c, title, _s, _m, _a in _cli_module().STARTER_ITEMS]
    assert "11 · Turn on night rest" in titles
    assert "10 · Set up an off-duty view" in titles
    assert "7 · Set up your Cockpit" in titles
    assert "8 · Set up an email digest" in titles
    assert "9 · Set up one routine" in titles
    assert "12 · Let Superboard learn from your threads" in titles
    assert not any("optional setup" in title.lower() for title in titles)
    cockpit = next(item for item in _cli_module().STARTER_ITEMS if "Cockpit" in item[1])
    assert cockpit[0] == "Jetzt"
    assert not cockpit[1].startswith("**")


def test_workspace_and_agent_setup_are_separate_concrete_outcomes() -> None:
    missions = {title: mission for _c, title, _s, mission, _a in _cli_module().STARTER_ITEMS}
    setup = missions["2 · Set up this workspace"]
    assert "context/README.md" in setup and "2–5 board topics" in setup
    assert "does not move its cards, threads or spend history" in setup
    agent = missions["6 · Check your agent and model setup"]
    assert "platform and run profile" in agent
    assert "Claude Code or the experimental macOS Codex runner" in agent
    assert "OpenCode is not a supported runner" in agent
    assert "tiny real run" in agent


def test_cockpit_setup_creates_extension_before_any_write() -> None:
    missions = {title.strip("*"): mission for _c, title, _s, mission, _a in _cli_module().STARTER_ITEMS}
    cockpit = missions["7 · Set up your Cockpit"]
    assert "FIRST ensure exactly one" in cockpit
    assert "Cockpit extension · Add a useful recurring action" in cockpit
    assert cockpit.index("FIRST") < cockpit.index("Inventory")
    assert "approval artifact before editing actions.json" in cockpit
    assert "Cockpit tab has appeared" in cockpit


def test_fresh_cockpit_is_hidden_and_configured_zones_are_compact() -> None:
    html = (HERE / "index.html").read_text(encoding="utf-8")
    assert "const cockpitHidden = () => !actionsDefs || actionsDefs.length === 0" in html
    assert 'set("cockpit", cockpitHidden())' in html
    assert '(cockpitHidden() && view === "cockpit")' in html
    assert 'box.hidden = !populated' in html and 'head.hidden = !populated' in html
    assert "your one-click actions" in html


def test_showcase_is_same_origin_and_fictional() -> None:
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    showcase = (HERE / "onboarding-showcase.html").read_text(encoding="utf-8")
    assert '"/onboarding-showcase"' in server_source
    assert "Fictional workspace" in showcase and "no personal data" in showcase
    assert "desktop layout" in showcase
    assert all(f'<div class="head">{column}</div>' in showcase for column in ("NOW", "NEXT", "BACKLOG"))
    assert 'id="threads"' in showcase
    assert "Sync activity tracker" in showcase
    assert 'id="off-duty"' in showcase
    assert "A card is a to-do first" in showcase
    assert "Keep one thread for one outcome" in showcase
    assert "cut the session when the outcome changes" not in showcase


def test_help_and_thread_lessons_are_task_shaped_and_agent_led() -> None:
    missions = {title: mission for _c, title, _s, mission, _a in _cli_module().STARTER_ITEMS}
    thread_lesson = missions["4 · Understand runs, threads and cache"]
    assert "current card as the example" in thread_lesson
    assert "prompt cache is only an efficiency" in thread_lesson
    assert "different outcome" in thread_lesson
    help_lesson = missions["5 · Find settings and get help"]
    assert "Superboard Agent entry" in help_lesson
    assert "plain workspace files" in help_lesson
    assert "not a complete settings screen" in help_lesson


def test_manual_cards_are_the_baseline_and_overlapping_saves_are_serialized() -> None:
    source = (HERE / "index.html").read_text(encoding="utf-8")
    assert "Enter adds a normal to-do" in source
    assert "manual is fine; the agent is optional" in source
    assert "every card here is a standing thread" not in source
    assert "if (saving && !retried)" in source
    assert "saveAgain = true" in source
    assert "if (saveAgain)" in source
    assert "const newItemId" in source
    assert "id: newItemId()" in source


def test_off_duty_uses_explicit_topics_and_keeps_unknown_topics_visible() -> None:
    missions = {title: mission for _c, title, _s, mission, _a in _cli_module().STARTER_ITEMS}
    mission = missions["10 · Set up an off-duty view"]
    assert "off_duty.hidden_topics" in mission and "off_duty.visible_topics" in mission
    assert "unknown or future topics remain visible" in mission
    source = (HERE / "index.html").read_text(encoding="utf-8")
    assert "off_duty_hidden_topics" in source
    assert "WORK_THEMES" not in source
