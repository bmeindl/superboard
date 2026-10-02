#!/usr/bin/env python3
"""Author provenance for Board turns and immutable sidecar corrections.

Direction (``kind``) and authorship are deliberately separate.  New turns carry a
small machine-readable comment; old turns are resolved conservatively.  Historical
sidecars can be corrected without changing their bytes through an append-only JSONL
journal next to ``gc-threads``.
"""
from __future__ import annotations

import argparse
import contextvars
import fcntl
import hashlib
import json
import re
import sys
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from markers import AUTHOR_META_PREFIX, REF_RE


AUTHORS = frozenset({"human", "agent", "system", "unknown"})
KINDS = frozenset({"ask", "reply", "sys", "done"})
META_PREFIX = AUTHOR_META_PREFIX
_META_RE = re.compile(r"<!--gc-meta:(\{.*?\})-->")
_SIDECAR_RE = REF_RE
_SOURCE_RE = re.compile(r"[A-Za-z0-9._-]+\.md")
# Optional display metadata (Faden cf2357146820, 24.09.): when the turn was written and,
# for agent replies, which model actually answered. Neither field affects authorship.
_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/@-]{1,64}")


@dataclass
class _ResolutionBatch:
    root: Path
    files: dict[str, list[Path]]
    records: list[dict[str, Any]] | None
    journal_error: str | None = None
    digests: dict[Path, str] = field(default_factory=dict)
    metadata: dict[Path, dict[str, str | None]] = field(default_factory=dict)


_ACTIVE_BATCH: contextvars.ContextVar[_ResolutionBatch | None] = contextvars.ContextVar(
    "gc_provenance_resolution_batch", default=None
)


@contextmanager
def resolution_batch(threads_dir: Path):
    """Snapshot recursive sidecar names and the journal for one resolver pass."""
    root = Path(threads_dir).resolve()
    files: dict[str, list[Path]] = {}
    if root.is_dir():
        for candidate in root.rglob("*.md"):
            resolved = candidate.resolve()
            if candidate.is_file() and resolved.is_relative_to(root):
                files.setdefault(candidate.name, []).append(candidate)
    try:
        records = _records(_journal_path(Path(threads_dir)))
        journal_error = None
    except (OSError, ValueError) as exc:
        records = None
        journal_error = str(exc)
    batch = _ResolutionBatch(root=root, files=files, records=records,
                             journal_error=journal_error)
    token = _ACTIVE_BATCH.set(batch)
    try:
        yield batch
    finally:
        _ACTIVE_BATCH.reset(token)


def _author(value: object) -> str:
    if not isinstance(value, str) or value not in AUTHORS:
        raise ValueError(f"invalid author: {value!r}")
    return value


def _turn_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("turn id must be 32 lowercase hexadecimal characters")
    return value


def _metadata(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed gc-meta JSON") from exc
    if (not isinstance(value, dict) or not {"id", "author"}.issubset(value)
            or not set(value).issubset({"id", "author", "source", "at", "model"})):
        raise ValueError("gc-meta must contain id and author, with optional source/at/model")
    meta = {"id": _turn_id(value["id"]), "author": _author(value["author"])}
    if "source" in value:
        meta["source"] = _source_basename(value["source"])
    if "at" in value:
        meta["at"] = turn_at(value["at"])
    if "model" in value:
        meta["model"] = turn_model(value["model"])
    return meta


def turn_at(value: object) -> str:
    if not isinstance(value, str) or not _AT_RE.fullmatch(value):
        raise ValueError("at must be 'YYYY-MM-DD HH:MM'")
    return value


def turn_model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL_RE.fullmatch(value):
        raise ValueError("model must be a short model id")
    return value


def encode(event: Mapping[str, Any]) -> str:
    """Serialize a turn body, prefixing metadata when author/id are present.

    Callers either supply both fields or neither; a half-labelled turn is rejected.
    An outer marker is always authoritative; marker examples inside user prose remain
    ordinary text and round-trip unchanged.
    """
    text = event.get("text")
    if not isinstance(text, str):
        raise TypeError("event text must be a string")
    has_author = "author" in event and event.get("author") is not None
    has_id = "turn_id" in event and event.get("turn_id") is not None
    if event.get("author") == "unknown" and event.get("turn_id") is None and META_PREFIX in text:
        return text
    if has_author != has_id:
        raise ValueError("author and turn_id must be supplied together")
    if not has_author:
        return text
    meta = {"id": _turn_id(event["turn_id"]), "author": _author(event["author"])}
    if event.get("source") is not None:
        meta["source"] = _source_basename(event["source"])
    if event.get("turn_at") is not None:
        meta["at"] = turn_at(event["turn_at"])
    if event.get("model") is not None:
        meta["model"] = turn_model(event["model"])
    return f"{META_PREFIX}{json.dumps(meta, separators=(',', ':'))}--> {text}"


def decode(text: str) -> dict[str, str | None]:
    """Parse and remove one structured marker anywhere in a turn/sidecar body.

    Invalid explicit metadata fails closed as ``unknown`` while preserving every
    source byte.  This lets legacy parsers keep loading a board without laundering a
    malformed declaration into an inferred human author.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text.startswith(META_PREFIX):
        return {"text": text}
    markers = list(_META_RE.finditer(text))
    if not markers or markers[0].start() != 0:
        return {"text": text, "author": "unknown", "turn_id": None}
    marker = markers[0]
    try:
        meta = _metadata(marker.group(1))
    except ValueError:
        return {"text": text, "author": "unknown", "turn_id": None}
    clean = text[:marker.start()] + text[marker.end():]
    if marker.start() == 0 and clean.startswith(" "):
        clean = clean[1:]
    decoded = {"text": clean, "author": meta["author"], "turn_id": meta["id"]}
    if "source" in meta:
        decoded["source"] = meta["source"]
    if "at" in meta:
        decoded["turn_at"] = meta["at"]
    if "model" in meta:
        decoded["model"] = meta["model"]
    return decoded


def decode_sidecar(text: str) -> dict[str, str | None]:
    """Read metadata only from its fixed slot after header/date and blank lines."""
    lines = text.splitlines()
    if len(lines) <= 4 or not lines[4].startswith(META_PREFIX):
        return {"text": text}
    marker_line = lines[4]
    if _META_RE.fullmatch(marker_line) is None:
        return {"text": text, "author": "unknown", "turn_id": None}
    decoded = decode(marker_line)
    result = {"text": text, "author": decoded.get("author"), "turn_id": decoded.get("turn_id")}
    if decoded.get("source") is not None:
        result["source"] = decoded["source"]
    return result


def new_turn(kind: str, text: str, author: str) -> dict[str, str]:
    """Build a new event with independent direction and declared authorship."""
    if kind not in KINDS:
        raise ValueError(f"invalid turn kind: {kind!r}")
    if not isinstance(text, str):
        raise TypeError("turn text must be a string")
    return {"kind": kind, "text": text, "author": _author(author), "turn_id": uuid.uuid4().hex,
            "turn_at": datetime.now().strftime("%Y-%m-%d %H:%M")}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_basename(value: object) -> str:
    if not isinstance(value, str) or not _SOURCE_RE.fullmatch(value):
        raise ValueError("source must be a sidecar basename ending in .md")
    return value


def _journal_path(threads_dir: Path) -> Path:
    return threads_dir.parent / "gc-provenance.jsonl"


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return _records_from_text(path.read_text())


def _records_from_text(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed provenance journal line {line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"invalid provenance journal line {line_number}")
        records.append(record)
    return records


def provenance_revision(threads_dir: Path) -> str:
    """Stable cache/checkpoint revision of the append-only correction journal."""
    journal = _journal_path(Path(threads_dir))
    return sha256_file(journal) if journal.exists() else hashlib.sha256(b"").hexdigest()


def _event_source(event: Mapping[str, Any]) -> str | None:
    if event.get("source") is not None:
        return _source_basename(event["source"])
    if event.get("source_basename") is not None:
        return _source_basename(event["source_basename"])
    turn_id = event.get("turn_id")
    if isinstance(turn_id, str) and re.fullmatch(r"[0-9a-f]{32}", turn_id):
        return None
    text = event.get("text", "")
    if isinstance(text, str) and (match := _SIDECAR_RE.search(text)):
        return match.group(1)
    return None


def _locate(threads_dir: Path, source: str, expected_sha256: str | None = None) -> Path:
    source = _source_basename(source)
    root = threads_dir.resolve()
    matches = [path for path in threads_dir.rglob(source)
               if path.is_file() and path.name == source and path.resolve().is_relative_to(root)]
    if expected_sha256 is not None:
        matches = [path for path in matches if sha256_file(path) == expected_sha256]
    if len(matches) != 1:
        reason = "not found" if not matches else "ambiguous"
        raise ValueError(f"sidecar {source!r} {reason}")
    return matches[0]


def resolve(event: Mapping[str, Any], threads_dir: Path | None = None) -> str:
    """Resolve author: hash-bound correction, explicit metadata, conservative legacy."""
    kind = event.get("kind")
    text = event.get("text", "")
    if not isinstance(text, str):
        raise TypeError("event text must be a string")

    explicit = event.get("author")
    parsed = {"text": text} if event.get("turn_id") else decode(text)
    if explicit is not None:
        try:
            explicit_author = _author(explicit)
        except ValueError as exc:
            warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
            return "unknown"
        if parsed.get("author") is not None and parsed["author"] != explicit_author:
            warnings.warn("event author conflicts with gc-meta", RuntimeWarning, stacklevel=2)
            return "unknown"
    else:
        explicit_author = parsed.get("author")

    source = _event_source(event)
    if source is not None:
        if threads_dir is None:
            import paths  # lazy: provenance remains safe for sidecar/server imports
            threads_dir = paths.THREADS
        threads_dir = Path(threads_dir)
        root = threads_dir.resolve()
        batch = _ACTIVE_BATCH.get()
        if batch is None or batch.root != root:
            with resolution_batch(threads_dir):
                return resolve(event, threads_dir)
        candidates = batch.files.get(source, [])
        if not candidates:
            warnings.warn(f"sidecar {source!r} not found", RuntimeWarning, stacklevel=2)
            return "unknown"
        if batch.records is None:
            warnings.warn(batch.journal_error or "invalid provenance journal",
                          RuntimeWarning, stacklevel=2)
            return "unknown"
        corrected: list[tuple[Path, dict[str, Any]]] = []
        for path in candidates:
            try:
                if path not in batch.digests:
                    batch.digests[path] = sha256_file(path)
            except OSError as exc:
                warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
                return "unknown"
            digest = batch.digests[path]
            matches = [record for record in batch.records
                       if record.get("source") == source and record.get("sha256") == digest]
            if matches:
                corrected.append((path, matches[-1]))
        if len(corrected) == 1:
            try:
                return _author(corrected[0][1].get("author"))
            except ValueError as exc:
                warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
                return "unknown"
        if len(corrected) > 1 or len(candidates) > 1:
            warnings.warn(f"sidecar {source!r} ambiguous", RuntimeWarning, stacklevel=2)
            return "unknown"
        path = candidates[0]
        # Metadata may live after the human-facing first header in a sidecar.
        try:
            if path not in batch.metadata:
                batch.metadata[path] = decode_sidecar(path.read_text())
        except OSError as exc:
            warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
            return "unknown"
        sidecar_meta = batch.metadata[path].get("author")
        if sidecar_meta is not None:
            if explicit_author is not None and sidecar_meta != explicit_author:
                return "unknown"
            return sidecar_meta
        if path.read_text().startswith("# Agent brief:"):
            return "agent"

    if explicit_author is not None:
        return explicit_author
    if kind == "reply":
        return "agent"
    if kind == "sys":
        return "system"
    return "unknown"


def append_correction(
    *, source: str, sha256: str, author: str, evidence: str,
    threads_dir: Path, item_id: str | None = None, correction_id: str | None = None,
    supersedes: str | None = None, timestamp: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and optionally append one hash-bound provenance correction."""
    source = _source_basename(source)
    author = _author(author)
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    threads_dir = Path(threads_dir)
    path = _locate(threads_dir, source, sha256)
    correction_id = correction_id or uuid.uuid4().hex
    _turn_id(correction_id)
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence is required")
    record: dict[str, Any] = {
        "id": correction_id, "source": source, "sha256": sha256, "author": author,
        "evidence": evidence.strip(),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    if item_id is not None:
        record["item_id"] = item_id
    if supersedes is not None:
        _turn_id(supersedes)
        record["supersedes"] = supersedes
    record["target"] = str(path)
    journal = _journal_path(threads_dir)
    if not dry_run:
        journal.parent.mkdir(parents=True, exist_ok=True)
        persisted = {key: value for key, value in record.items() if key != "target"}
        with journal.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = _records_from_text(handle.read())
            if any(row.get("id") == correction_id for row in existing):
                raise ValueError("correction id already exists")
            if supersedes is not None and sum(row.get("id") == supersedes for row in existing) != 1:
                raise ValueError("superseded correction id not found or ambiguous")
            if sha256_file(path) != sha256:
                raise ValueError("sidecar changed before correction append")
            handle.seek(0, 2)
            handle.write(json.dumps(persisted, separators=(",", ":")) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def undo_correction(correction_id: str, *, threads_dir: Path, evidence: str,
                    dry_run: bool = False) -> dict[str, Any]:
    """Undo by appending an ``unknown`` correction that supersedes the target."""
    _turn_id(correction_id)
    matches = [record for record in _records(_journal_path(Path(threads_dir)))
               if record.get("id") == correction_id]
    if len(matches) != 1:
        raise ValueError("correction id not found or ambiguous")
    target = matches[0]
    return append_correction(
        source=target["source"], sha256=target["sha256"], author="unknown",
        evidence=evidence, threads_dir=Path(threads_dir), item_id=target.get("item_id"),
        supersedes=correction_id, dry_run=dry_run,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--source", required=True)
    apply_parser.add_argument("--sha256", required=True)
    apply_parser.add_argument("--author", choices=sorted(AUTHORS), required=True)
    apply_parser.add_argument("--evidence", required=True)
    apply_parser.add_argument("--item-id")
    apply_parser.add_argument("--dry-run", action="store_true")
    undo_parser = sub.add_parser("undo")
    undo_parser.add_argument("--id", required=True)
    undo_parser.add_argument("--evidence", required=True)
    undo_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "apply":
            result = append_correction(source=args.source, sha256=args.sha256,
                                       author=args.author, evidence=args.evidence,
                                       item_id=args.item_id, threads_dir=args.threads_dir,
                                       dry_run=args.dry_run)
        else:
            result = undo_correction(args.id, threads_dir=args.threads_dir,
                                     evidence=args.evidence, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
