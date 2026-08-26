"""The version number measures the KERNEL, not the content of one installation.

Why this is tested: `.json` is in `CODE_SUFFIXES` because real code lives in JSON too,
so content files used to count as code — a new cockpit action would raise the version
of the published package. The exception is a quiet rule; without a test, losing it only
shows up weeks later in a number nobody can explain any more.
"""
from __future__ import annotations

from pathlib import Path

import bump


def _numstat(*zeilen: str) -> str:
    return "\n".join(zeilen) + "\n"


def test_content_does_not_count():
    churn, files = bump.numstat_churn(_numstat(
        "40\t3\tsuperboard/actions.json",
        "12\t0\tsuperboard/rituals.json",
        "7\t0\tsuperboard/rituale.json",
        "5\t5\tsuperboard/board.config.json",
    ))
    assert (churn, files) == (0, [])


def test_code_still_counts():
    churn, files = bump.numstat_churn(_numstat(
        "40\t3\tsuperboard/actions.json",
        "30\t10\tsuperboard/server.py",
    ))
    assert churn == 30                      # max(added, deleted), content excluded
    assert files == ["server.py"]


def test_same_name_elsewhere_is_content_too():
    """The filter matches the file name, not the path — same class of content
    whether it sits in the package folder or a subfolder."""
    churn, _ = bump.numstat_churn(_numstat("9\t0\tsuperboard/sandbox/actions.json"))
    assert churn == 0


def test_major_is_never_automatic():
    """A big feat is a suggestion, never a main number on its own."""
    level, _ = bump.decide("feat(ui): big one", bump.MAJOR_HINT_LINES * 2, None)
    assert level == "minor"


def test_internal_bump_does_not_overwrite_public_package_version(
    tmp_path: Path, monkeypatch,
) -> None:
    server = tmp_path / "server.py"
    project = tmp_path / "pyproject.toml"
    server.write_text('VERSION = "6.20.3"\n', encoding="utf-8")
    project.write_text('version = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setattr(bump, "SERVER", server)

    bump.write_version("6.20.4")

    assert server.read_text(encoding="utf-8") == 'VERSION = "6.20.4"\n'
    assert project.read_text(encoding="utf-8") == 'version = "0.1.0"\n'
