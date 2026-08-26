#!/usr/bin/env python3
"""Leak scan — the gate that keeps private content out of this repo.

TWO SURFACES, and the difference is the whole point:

    leak_scan.py              the WORKING TREE — what an editor shows.
    leak_scan.py --history    every BLOB in the history — what `git show` shows.

A clone carries the second one. Cleaning a file in a later commit does not clean
the commit that introduced it, so a working-tree scan will happily call a repo
clean while an old commit still hands the content to anyone who clones. History
is the surface that goes public, so history is the surface the push gate checks.

TWO PATTERN SETS, for a reason worth stating:

    built-in    generic and harmless to publish — home paths, e-mail addresses,
                private IP ranges, credential shapes. Useful to anyone.
    local       your own vocabulary: names, employer, colleagues, hostnames.
                Read from .leakpatterns (gitignored) or $LEAK_PATTERNS.

The second set is not in this file on purpose. A committed list of the words you
are scrubbing out IS the leak — it publishes your employer, your infrastructure
and your colleagues' names in one reviewable place, which is worse than most of
what it would have caught. So the list stays local and this file stays generic.
Every run prints which sets are active; a missing local file degrades the scan
loudly, never silently.

Exit 0 = clean, exit 1 = findings. Philosophy: the scan is the NET, not the bar —
when in doubt, content stays out of the repo entirely rather than being redacted
around ("rather one thing too few").
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Generic patterns: things that should not sit in ANY public repo, phrased so that
# the list itself gives nothing away. Pattern → label. Case-insensitive unless the
# pattern uses (?-i:).
PATTERNS: list[tuple[str, str]] = [
    (r"/Users/[a-z0-9._-]+|/home/(?!runner\b)[a-z0-9._-]+", "absolute home path"),
    # Public no-reply identities are release metadata, not personal contact data.
    (r"(?<![a-z0-9._%+-])(?!noreply@github\.com\b)[a-z0-9._%+-]+@(?!anthropic\.com|users\.noreply\.github\.com|example\.(com|org))[a-z0-9.-]+\.[a-z]{2,}",
     "email address"),
    (r"\b(10|192\.168|172\.(1[6-9]|2\d|3[01])|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"
     r"\.\d{1,3}\.\d{1,3}\b", "private network address"),
    # real AWS SSO org ids only (d-<10 hex>); placeholders/example URLs are fine
    (r"d-[0-9a-f]{10}\.awsapps\.com", "private infrastructure (aws sso)"),
    (r"(?-i:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9_-]{24,}|"
     r"xox[baprs]-[A-Za-z0-9-]{10,})", "credential"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
]

# file → labels that are deliberately allowed there.
ALLOW: dict[str, set[str]] = {
    "pyproject.toml": {"email address"},
}

# Where the private vocabulary lives. Gitignored; a symlink into a private repo is
# a perfectly good answer, since only this machine ever needs to resolve it.
LOCAL_PATTERNS = Path(os.environ.get("LEAK_PATTERNS", ROOT / ".leakpatterns"))

BINARY_SUFFIXES = {".png", ".icns", ".sqlite", ".jsonl", ".pyc"}

# A blob larger than this is data, not prose — scanning it costs more than it finds.
MAX_BLOB_BYTES = 1_000_000


def load_local_patterns() -> tuple[list[tuple[str, str]], int]:
    """Extra patterns and allow-entries from the local file, if there is one.

    Tab-separated, '#' comments, two line shapes:
        <regex>          <TAB> <label>
        allow:<path>     <TAB> <label>
    """
    if not LOCAL_PATTERNS.is_file():
        return [], 0
    extra: list[tuple[str, str]] = []
    allows = 0
    for raw in LOCAL_PATTERNS.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        first, _, label = line.partition("\t")
        first, label = first.strip(), label.strip()
        if not label:
            continue
        if first.startswith("allow:"):
            ALLOW.setdefault(first[len("allow:"):].strip(), set()).add(label)
            allows += 1
        else:
            extra.append((first, label))
    return extra, allows


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout


def scan_text(rel: str, text: str, compiled: list[tuple[re.Pattern[str], str]]) -> list[str]:
    """Every finding in one file's text, as printable lines."""
    allowed = ALLOW.get(rel, set())
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rx, label in compiled:
            if label in allowed:
                continue
            m = rx.search(line)
            if m:
                out.append(f"{rel}:{lineno}: [{label}] {m.group(0)!r} — {line.strip()[:120]}")
    return out


# ---------------------------------------------------------------- working tree

def worktree_files() -> list[tuple[str, str]]:
    """(path, text) for every tracked or untracked-but-not-ignored file. Ignored
    files are out of scope by definition — they are what .gitignore is for."""
    result: list[tuple[str, str]] = []
    for rel in _git("ls-files", "--cached", "--others", "--exclude-standard").splitlines():
        if not rel:
            continue
        path = ROOT / rel
        if path.suffix in BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            result.append((rel, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return result


# -------------------------------------------------------------------- history

def _read_blobs(shas: list[str]) -> dict[str, bytes]:
    """Contents of many blobs in ONE git process — a per-object `git cat-file` call
    turns a pre-push hook into a coffee break on any repo with real history."""
    if not shas:
        return {}
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    raw, _ = proc.communicate(("\n".join(shas) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            break
        header = raw[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3:          # "<sha> missing" — object not in this repo
            continue
        sha, kind, size = header[0], header[1], int(header[2])
        if kind == "blob" and size <= MAX_BLOB_BYTES:
            blobs[sha] = raw[pos:pos + size]
        pos += size + 1              # payload + its trailing newline
    return blobs


def history_files(revs: list[str]) -> list[tuple[str, str]]:
    """(path, text) for every blob reachable from `revs` — i.e. everything a clone
    of that range could ever `git show`, not just what the tip happens to contain.

    Deduplicated by blob sha: a file that survives fifty commits unchanged is one
    blob and is scanned once."""
    pairs: set[tuple[str, str]] = set()
    for line in _git("rev-list", "--objects", *revs).splitlines():
        sha, _, rel = line.partition(" ")
        if rel and not rel.endswith("/") and Path(rel).suffix not in BINARY_SUFFIXES:
            pairs.add((sha, rel))
    blobs = _read_blobs(sorted({sha for sha, _ in pairs}))
    result: list[tuple[str, str]] = []
    for sha, rel in sorted(pairs, key=lambda p: p[1]):
        data = blobs.get(sha)
        if data is None:
            continue
        try:
            result.append((rel, data.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return result


def history_metadata(revs: list[str]) -> list[tuple[str, str]]:
    """Author, committer, and message metadata reachable from ``revs``.

    Commit objects are public clone data too. Scanning only their blobs misses a
    personal e-mail in an otherwise clean one-commit projection.
    """
    identities = _git("log", "--format=commit %H%nAuthor: %an <%ae>%nCommitter: %cn <%ce>",
                      *revs)
    messages = _git("log", "--format=commit %H%n%B", *revs)
    return [
        (".git-commit-identities", identities),
        (".git-commit-messages", messages),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", nargs="*", metavar="REV",
                    help="scan git history instead of the working tree. Without "
                         "arguments: all of HEAD. Anything after it is handed to "
                         "`git rev-list` verbatim, flags included — the pre-push hook "
                         "uses `--history <sha> --not --remotes` to scan exactly the "
                         "commits that are about to become public.")
    args, passthrough = ap.parse_known_args()
    if passthrough and args.history is None:
        ap.error(f"unrecognized arguments: {' '.join(passthrough)}")

    extra, allows = load_local_patterns()
    compiled = [(re.compile(p, re.IGNORECASE), label) for p, label in [*PATTERNS, *extra]]

    if extra or allows:
        print(f"patterns: {len(PATTERNS)} built-in + {len(extra)} local "
              f"({allows} allow-entries)")
    else:
        print(f"patterns: {len(PATTERNS)} built-in only — no local pattern file at "
              f"{LOCAL_PATTERNS}.\n          Generic scan. Names, employer and "
              f"hostnames are NOT being checked.")

    if args.history is None:
        surface, files = "working tree", worktree_files()
    else:
        revs = [*args.history, *passthrough] or ["HEAD"]
        surface = f"history ({' '.join(revs)})"
        files = [*history_files(revs), *history_metadata(revs)]

    findings = 0
    for rel, text in files:
        for line in scan_text(rel, text, compiled):
            findings += 1
            print(line)

    if findings:
        print(f"\nLEAK SCAN [{surface}]: {findings} finding(s). NOT clean.")
        if args.history is not None:
            print("A finding in history is public the moment this is pushed, even if the "
                  "working tree is clean.\nFix the content, then rewrite the history that "
                  "carries it (orphan commit / squash) — editing a later commit is not "
                  "enough.")
        return 1
    print(f"LEAK SCAN [{surface}]: clean ({len(files)} file(s) scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
