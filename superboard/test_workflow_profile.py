"""The multi-agent profile: Claude Code's `Workflow` tool, switchable per item.

Background: `Workflow` sits in UNUSED_TOOLS because its schema costs ~8k tokens in EVERY
turn — measured on the wire, roughly a tenth of a median deep run. It stays off as a
DEFAULT only: a headless run should still be able to fan out for genuinely deep work.
The switch is the `opus-multi` profile in the board's run-profile dropdown.

What is protected here is the coupling of four things that each break silently on their
own: the tool list, the opt-in sentence in the prompt, the dropdown entry and the
stall watch.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import gc_runner  # noqa: E402


def test_workflow_stays_off_in_the_normal_case() -> None:
    """Every ordinary profile does NOT pay the 8k tokens."""
    for profile in ("", "opus", "opus-xhigh", "sonnet", "fable"):
        assert "Workflow" in gc_runner.disallowed_tools(profile), profile
        assert gc_runner.disallowed_tools(profile) == gc_runner.UNUSED_TOOLS


def test_the_multi_profile_frees_only_workflow() -> None:
    """The other three tools stay out — headless they are genuinely unreachable."""
    tools = gc_runner.disallowed_tools("opus-multi")
    assert "Workflow" not in tools
    assert set(tools) == set(gc_runner.UNUSED_TOOLS) - {"Workflow"}


def test_the_opt_in_sentence_hangs_on_the_multi_profile_and_only_at_the_end() -> None:
    """By its own schema the tool fires ONLY on an explicit opt-in by the user. Choosing
    the profile IS that opt-in, so it has to reach the agent in the prompt. It is appended
    at the END, otherwise the cached prompt prefix breaks."""
    prompt = "Task: anything."
    assert gc_runner.apply_workflow_opt_in(prompt, "opus") == prompt
    multi = gc_runner.apply_workflow_opt_in(prompt, "opus-multi")
    assert multi.startswith(prompt)
    assert "Workflow" in multi and "opt-in" in multi


def test_multi_profiles_are_real_run_profiles() -> None:
    """A profile the server does not know would be rejected with a 400."""
    assert gc_runner.WORKFLOW_PROFILES <= set(gc_runner.RUN_PROFILES)
    assert gc_runner.resolve_profile("opus-multi") == ("opus", "xhigh")


def test_the_stall_watch_tolerates_a_running_workflow() -> None:
    """A workflow returns IMMEDIATELY with a task id and then works in the background:
    no open tool, yet legitimately long silence. Without this flag the idle clock would
    kill the run in the middle of the fan-out."""
    tail = gc_runner.StreamTail(Path("/nonexistent"))
    assert tail.state["workflow"] is False
    tail._absorb('{"type":"assistant","message":{"content":['
                 '{"type":"tool_use","id":"t1","name":"Workflow","input":{}}]}}')
    assert tail.state["workflow"] is True
    # and stays set once the tool is long back — that is exactly when it counts
    tail._absorb('{"type":"user","message":{"content":['
                 '{"type":"tool_result","tool_use_id":"t1"}]}}')
    assert tail.state["busy"] == 0 and tail.state["workflow"] is True


def test_the_dropdown_knows_multi_and_remembers_it_per_item_only() -> None:
    """Multi-agent is a choice for ONE item, never the silent global default — the same
    clause the per-item profiles already carry."""
    src = (HERE / "index.html").read_text(encoding="utf-8")
    block = re.search(r"const RUN_PROFILES = \[(.*?)\];", src, re.S)
    assert block, "RUN_PROFILES no longer found in index.html"
    assert '"opus-multi"' in block.group(1)
    per_item = re.search(r"const isPerItemProfile = .*", src).group(0)
    assert 'endsWith("-multi")' in per_item
