from __future__ import annotations

import re

from scripts import leak_scan


def _compiled() -> list[tuple[re.Pattern[str], str]]:
    return [(re.compile(pattern, re.IGNORECASE), label)
            for pattern, label in leak_scan.PATTERNS]


def test_commit_metadata_rejects_personal_email_and_allows_public_noreply() -> None:
    compiled = _compiled()

    assert leak_scan.scan_text(
        ".git-commit-metadata",
        "Author: Person <person" + "@gmail.com>",
        compiled,
    )
    assert not leak_scan.scan_text(
        ".git-commit-metadata",
        "Author: Person <123+person@users.noreply.github.com>",
        compiled,
    )


def test_history_metadata_reads_reachable_commit_objects(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        calls.append(args)
        return "Author: Person <123+person@users.noreply.github.com>\n"

    monkeypatch.setattr(leak_scan, "_git", fake_git)

    metadata = leak_scan.history_metadata(["HEAD", "--not", "--remotes"])

    assert metadata[0][0] == ".git-commit-identities"
    assert metadata[1][0] == ".git-commit-messages"
    assert "users.noreply.github.com" in metadata[0][1]
    assert calls[0][-3:] == ("HEAD", "--not", "--remotes")
