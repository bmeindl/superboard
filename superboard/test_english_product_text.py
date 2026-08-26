"""Regression tripwires for the board's single-language product surface.

This deliberately checks shipped text sinks, not every source string: comments,
historical fixture data, and frozen protocol markers may remain German forever.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import gc_runner  # noqa: E402
import receipt  # noqa: E402
import registries  # noqa: E402
import server  # noqa: E402
import sidecar  # noqa: E402
import onboarding  # noqa: E402


GERMAN_PRODUCT_TOKENS = (
    " fehlt", "kein ", "keine ", "läuft", " bereits", "muss ", "darf ",
    "unbekannt", "stopp", "neustart", "wartet", "geblockt", "board hat",
    "heute schon", "leer sein", "fehlgeschlagen", "nicht erreichbar",
    "ausführen", "abbrechen", "schließen", "öffnen", "speichern", "anzeigen",
    "weniger", "gestern", "morgen", "tage", "woche", "feierabend",
)


def _has_german_product_token(text: str) -> bool:
    lower = text.lower()
    words = r"a-zäöüß"
    return any(re.search(rf"(?<![{words}]){re.escape(token.strip())}(?![{words}])", lower)
               for token in GERMAN_PRODUCT_TOKENS) or any(
        char in text for char in "ÄÖÜäöüß"
    )


class _ChromeParser(HTMLParser):
    """Collect only shipped HTML text/attributes, never comments, CSS, or JavaScript."""

    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
        if not self.skip_depth:
            self.values.extend(value or "" for key, value in attrs
                               if key in {"title", "placeholder", "aria-label"})

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data.strip():
            self.values.append(data.strip())


def test_browser_chrome_literals_are_english() -> None:
    """Curated tripwire for static chrome and direct JavaScript render sinks."""
    source = (HERE / "index.html").read_text(encoding="utf-8")
    assert "`KW ${" not in source
    parser = _ChromeParser()
    parser.feed(source)

    # Dynamic copy reaches the DOM through these direct sinks. This is deliberately
    # narrower than scanning every JS literal: column keys and protocol markers are data.
    js_values = [match.group("value") for match in re.finditer(
        r"(?:\.textContent|\.innerText|\.title|\.placeholder)\s*=\s*"
        r"(?P<quote>['\"`])(?P<value>.*?)(?P=quote)", source, re.S
    )]
    js_values += [match.group("value") for match in re.finditer(
        r"\b(?:status|btn|mi|faShow)\(\s*(?P<quote>['\"`])"
        r"(?P<value>.*?)(?P=quote)", source, re.S
    )]
    findings = [value for value in parser.values + js_values
                if _has_german_product_token(value)]
    assert findings == []


def test_agent_start_keeps_persistent_feedback_in_the_thread() -> None:
    source = (HERE / "index.html").read_text(encoding="utf-8")
    assert 'refreshOverlayRun(it, "for_gc");' in source
    assert 'sub2.dataset.gcId = it.id || "";' in source
    assert "progress and the reply stay in this thread" in source
    assert "${running.length} active" in source
    assert 'textContent = "Build " + data.version' in source
    assert 'const openId = overlayOpen ? document.getElementById("gc-ov-status")?.dataset.gcId' in source
    assert "openGcOverlay(hit[0], hit[1]);" in source


def test_visible_dates_use_unambiguous_iso_order() -> None:
    source = (HERE / "index.html").read_text(encoding="utf-8")
    assert "`${ts.getDate()}.${ts.getMonth() + 1}. ${hm}`" not in source
    assert "`${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}." not in source
    assert "it.gc_last.replace(/(\\d{4})-(\\d{2})-(\\d{2})/" not in source


def test_public_api_diagnostics_are_english() -> None:
    """Inspect only payload fields that the browser renders as human text."""
    source = (HERE / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in {"error", "note", "empty", "msg"}):
                continue
            if not isinstance(value, (ast.Constant, ast.JoinedStr)):
                continue
            literal = ast.get_source_segment(source, value) or ""
            if _has_german_product_token(literal):
                findings.append((node.lineno, str(key.value), literal))
    assert findings == []


def test_generated_artifacts_are_english_but_protocol_stays_frozen() -> None:
    pending = {
        "addr": {"id": "abcabcabcabc", "name": "Dev", "col": "Jetzt"},
        "title": "Example", "body": [], "thread": [], "last_ask": "Continue.",
        "session": "", "stages": [], "gc_last": "",
    }
    prompt = gc_runner.build_prompt(pending, resume=False)
    assert "You are the Superboard Agent" in prompt
    assert "Task: Handle the latest" in prompt
    assert "### Working state" in prompt
    assert "Du bist der Board-Agent" not in prompt
    for marker in ("→ full text:", "→ full reply:"):
        assert marker in gc_runner.PROMPT_CONTRACT

    assert server.ARBEITSSTAND_HEAD_RE.match("### Working state")
    assert server.ARBEITSSTAND_HEAD_RE.match("### Arbeitsstand")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = sidecar.write_sidecar("abcabcabcabc", "Example", "Full text", root)
        assert path.read_text(encoding="utf-8").startswith("# Board agent reply: Example")
        rendered = receipt._fmt_facts(
            "abcabcabcabc", "Example",
            {"ok": True, "denials": [], "context_tokens": 0, "usage_summary": {}},
            {"commits": [], "dirty": [], "dirty_new": [], "dirty_pre": 0}, time.time(),
        )
        assert "# Run receipt" in rendered and "**Result:** ok" in rendered


def test_fresh_workspace_and_onboarding_use_one_public_vocabulary() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("superboard_cli_english", HERE / "__main__.py")
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    board = cli._starter_board()
    for heading in ("### Now", "### Next", "### Backlog", "# To discuss", "# Notes"):
        assert heading in board
    for leaked in ("### Jetzt", "### Bald", "### Geparkt", "# Personen", "# Notizen"):
        assert leaked not in board
    public = "\n".join(
        " ".join((title, short, mission, ask))
        for _column, title, short, mission, ask in onboarding.STARTER_ITEMS
    )
    assert "Now/Soon/Parked" not in public and "Now / Soon / Parked" not in public
    showcase = (HERE / "onboarding-showcase.html").read_text(encoding="utf-8")
    guide = (HERE.parent / "docs" / "USING-SUPERBOARD.md").read_text(encoding="utf-8")
    assert "Now / Next / Backlog" in showcase
    assert "| Now | Next | Backlog |" in guide


def test_checked_in_registries_are_valid_and_user_fields_are_english() -> None:
    actions, action_errors = registries.load_actions(HERE / "actions.json")
    rituals, ritual_errors = registries.load_rituals(HERE / "rituals.json")
    assert action_errors == [] and actions == []
    # rituals.json ships empty in this fresh extraction (no baked-in rituals);
    # only assert it loads clean, not that it has entries.
    assert ritual_errors == []

    # The long prompts intentionally cite persisted German headings and marker values;
    # the chrome-facing fields have no such exception.
    chrome = [str(a.get(field, "")) for a in actions for field in ("label", "rhythm", "status")]
    chrome += [str(r.get("title", "")) for r in rituals["rituale"].values()]
    assert all(not _has_german_product_token(value) for value in chrome)


def test_registry_files_remain_json() -> None:
    for name in ("actions.json", "rituals.json"):
        assert isinstance(json.loads((HERE / name).read_text(encoding="utf-8")), dict)
