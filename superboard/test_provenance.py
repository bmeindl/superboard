import hashlib
import json
import re
from pathlib import Path

import pytest

import provenance


def test_new_turn_encode_decode_roundtrip():
    event = provenance.new_turn("ask", "one\ntwo", "agent")
    encoded = provenance.encode(event)
    assert encoded.startswith('<!--gc-meta:{"id":"')
    assert provenance.decode(encoded) == {
        "text": "one\ntwo", "author": "agent", "turn_id": event["turn_id"],
        "turn_at": event["turn_at"],
    }
    assert event["kind"] == "ask"


def test_optional_source_encode_decode_roundtrip_and_validation():
    event = provenance.new_turn("ask", "summary", "human")
    event["source"] = "item-20260906-120000-abcd.md"
    assert provenance.decode(provenance.encode(event)) == {
        "text": "summary", "author": "human", "turn_id": event["turn_id"],
        "source": event["source"], "turn_at": event["turn_at"],
    }
    event["source"] = "../escape.md"
    with pytest.raises(ValueError, match="basename"):
        provenance.encode(event)


def test_encode_legacy_and_reject_partial_or_invalid_metadata():
    assert provenance.encode({"text": "legacy"}) == "legacy"
    assert provenance.decode("legacy") == {"text": "legacy"}
    with pytest.raises(ValueError):
        provenance.encode({"text": "x", "author": "human"})
    with pytest.raises(ValueError):
        provenance.new_turn("ask", "x", "robot")
    malformed = '<!--gc-meta:{"id":"bad","author":"human"}--> x'
    assert provenance.decode(malformed) == {
        "text": malformed, "author": "unknown", "turn_id": None
    }
    malformed = "<!--gc-meta:nope--> x"
    decoded = provenance.decode(malformed)
    assert decoded == {
        "text": malformed, "author": "unknown", "turn_id": None
    }
    assert provenance.encode(decoded) == malformed


def test_decode_finds_metadata_after_sidecar_header():
    turn_id = "a" * 32
    body = f'# Agent brief: T\n\n*date · item*\n\n<!--gc-meta:{{"id":"{turn_id}","author":"agent"}}-->\ntext'
    assert provenance.decode(body) == {"text": body}
    assert provenance.decode_sidecar(body) == {
        "text": body, "author": "agent", "turn_id": turn_id
    }


def test_quoted_metadata_is_plain_prose():
    quoted = 'A quote: <!--gc-meta:{"id":"' + "a" * 32 + '","author":"human"}-->'
    assert provenance.decode(quoted) == {"text": quoted}


def test_authored_text_can_start_with_a_literal_metadata_example():
    inner = '<!--gc-meta:{"id":"' + "e" * 32 + '","author":"human"}--> tutorial'
    event = provenance.new_turn("ask", inner, "agent")
    decoded = provenance.decode(provenance.encode(event))
    assert decoded == {"text": inner, "author": "agent", "turn_id": event["turn_id"],
                       "turn_at": event["turn_at"]}


def test_turn_time_and_model_roundtrip_and_fail_closed():
    """Faden cf2357146820: every new turn carries its own time; replies may name the model."""
    event = provenance.new_turn("reply", "done", "agent")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", event["turn_at"])
    event["model"] = "claude-opus-5-5"
    decoded = provenance.decode(provenance.encode(event))
    assert decoded["turn_at"] == event["turn_at"] and decoded["model"] == "claude-opus-5-5"
    event["model"] = "bad model <script>"
    with pytest.raises(ValueError, match="model"):
        provenance.encode(event)
    forged = '<!--gc-meta:{"id":"' + "a" * 32 + '","author":"human","at":"yesterday"}--> x'
    assert provenance.decode(forged)["author"] == "unknown"


def test_sidecar_ignores_metadata_examples_outside_designated_slot():
    marker = '<!--gc-meta:{"id":"' + "f" * 32 + '","author":"human"}-->'
    body = f"# Agent brief: T\n\n*date*\n\nplain\n\nExample: {marker}"
    assert provenance.decode_sidecar(body) == {"text": body}


def test_resolve_explicit_and_conservative_legacy():
    assert provenance.resolve({"kind": "ask", "text": "hello", "author": "human"}) == "human"
    assert provenance.resolve({"kind": "reply", "text": "hello"}) == "agent"
    assert provenance.resolve({"kind": "sys", "text": "hello"}) == "system"
    assert provenance.resolve({"kind": "ask", "text": "hello"}) == "unknown"


def test_legacy_agent_brief_is_excluded(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "abc-20260906-120000-abcd.md"
    (threads / source).write_text("# Agent brief: title\n\nagent words\n")
    event = {"kind": "ask", "text": f"summary → voller Text: inbox/gc-threads/{source}"}
    assert provenance.resolve(event, threads) == "agent"


def test_new_turn_does_not_treat_typed_sidecar_reference_as_source(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    event = provenance.new_turn(
        "ask", "Example → voller Text: inbox/gc-threads/missing.md", "human"
    )
    assert provenance.resolve(event, threads) == "human"


def test_new_turn_with_explicit_missing_source_fails_closed(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    event = provenance.new_turn("ask", "summary", "human")
    event["source"] = "missing.md"
    with pytest.warns(RuntimeWarning, match="not found"):
        assert provenance.resolve(event, threads) == "unknown"


def test_sidecar_structured_metadata_beats_legacy_header(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "abc-20260906-120000-abcd.md"
    meta = '<!--gc-meta:{"id":"' + "b" * 32 + '","author":"human"}-->'
    (threads / source).write_text(f"# Agent brief: title\n\n*date*\n\n{meta}\nhuman words\n")
    event = {"kind": "ask", "text": f"summary → voller Text: inbox/gc-threads/{source}",
             "source": source}
    assert provenance.resolve(event, threads) == "human"


def test_correction_apply_resolve_and_undo_without_source_mutation(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "item-20260906-120000-abcd.md"
    path = threads / source
    original = b"# the owner turn: title\n\nagent-authored brief\n"
    path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    event = {"kind": "ask", "text": f"summary → voller Text: inbox/gc-threads/{source}"}

    correction = provenance.append_correction(
        source=source, sha256=digest, author="agent", evidence="review.md",
        item_id="item", threads_dir=threads, timestamp="2026-09-06T12:00:00+00:00",
    )
    assert path.read_bytes() == original
    assert provenance.resolve(event, threads) == "agent"
    revision = provenance.provenance_revision(threads)
    undo = provenance.undo_correction(correction["id"], threads_dir=threads, evidence="reverted")
    assert undo["author"] == "unknown"
    assert undo["supersedes"] == correction["id"]
    assert provenance.resolve(event, threads) == "unknown"
    assert provenance.provenance_revision(threads) != revision
    assert path.read_bytes() == original
    rows = [json.loads(line) for line in (tmp_path / "gc-provenance.jsonl").read_text().splitlines()]
    assert len(rows) == 2


def test_dry_run_does_not_write_and_hash_mismatch_fails(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "item.md"
    path = threads / source
    path.write_text("words")
    digest = provenance.sha256_file(path)
    result = provenance.append_correction(source=source, sha256=digest, author="agent",
                                          evidence="case", threads_dir=threads, dry_run=True)
    assert result["target"] == str(path)
    assert not (tmp_path / "gc-provenance.jsonl").exists()
    with pytest.raises(ValueError, match="not found"):
        provenance.append_correction(source=source, sha256="0" * 64, author="agent",
                                     evidence="case", threads_dir=threads)


def test_archive_resolution_requires_unique_basename_and_hash(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    (threads / "archive-a").mkdir(parents=True)
    (threads / "archive-b").mkdir()
    source = "same.md"
    first = threads / "archive-a" / source
    second = threads / "archive-b" / source
    first.write_text("first")
    second.write_text("second")
    event = {"kind": "ask", "text": "x", "source": source}
    with pytest.warns(RuntimeWarning, match="ambiguous"):
        assert provenance.resolve(event, threads) == "unknown"
    digest = provenance.sha256_file(first)
    provenance.append_correction(source=source, sha256=digest, author="human",
                                 evidence="audit", threads_dir=threads)
    assert provenance.resolve(event, threads) == "human"


def test_source_must_be_basename(tmp_path: Path):
    with pytest.raises(ValueError, match="basename"):
        provenance.append_correction(source="../escape.md", sha256="0" * 64,
                                     author="human", evidence="x", threads_dir=tmp_path)


def test_missing_sidecar_and_broken_journal_fail_closed(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    missing = {"kind": "ask", "text": "x", "source": "missing.md"}
    with pytest.warns(RuntimeWarning, match="not found"):
        assert provenance.resolve(missing, threads) == "unknown"
    path = threads / "there.md"
    path.write_text("legacy")
    (tmp_path / "gc-provenance.jsonl").write_text("{broken\n")
    with pytest.warns(RuntimeWarning, match="malformed"):
        assert provenance.resolve({"kind": "ask", "text": "x", "source": path.name}, threads) == "unknown"


def test_file_metadata_conflict_fails_closed(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "turn.md"
    marker = '<!--gc-meta:{"id":"' + "c" * 32 + '","author":"agent"}-->'
    (threads / source).write_text(f"# Agent brief: x\n\n*date*\n\n{marker}\nbody")
    event = provenance.new_turn("ask", "summary", "human")
    event["source"] = source
    assert provenance.resolve(event, threads) == "unknown"


def test_duplicate_correction_id_is_rejected(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "turn.md"
    path = threads / source
    path.write_text("body")
    digest = provenance.sha256_file(path)
    correction_id = "d" * 32
    args = dict(source=source, sha256=digest, author="agent", evidence="audit",
                threads_dir=threads, correction_id=correction_id)
    provenance.append_correction(**args)
    with pytest.raises(ValueError, match="already exists"):
        provenance.append_correction(**args)


def test_resolution_batch_snapshots_files_and_corrections(tmp_path: Path):
    threads = tmp_path / "gc-threads"
    archive = threads / "archive"
    archive.mkdir(parents=True)
    source = "archived.md"
    path = archive / source
    path.write_text("# the owner turn: x\n\nlegacy")
    event = {"kind": "ask", "text": "x", "source": source}
    digest = provenance.sha256_file(path)

    with provenance.resolution_batch(threads):
        assert provenance.resolve(event, threads) == "unknown"
        provenance.append_correction(source=source, sha256=digest, author="agent",
                                     evidence="audit", threads_dir=threads)
        # A batch is a deliberate immutable snapshot.
        assert provenance.resolve(event, threads) == "unknown"
        new_event = {"kind": "ask", "text": "x", "source": "new.md"}
        (threads / "new.md").write_text("# Agent brief: x\n")
        with pytest.warns(RuntimeWarning, match="not found"):
            assert provenance.resolve(new_event, threads) == "unknown"

    with provenance.resolution_batch(threads):
        assert provenance.resolve(event, threads) == "agent"
        assert provenance.resolve(new_event, threads) == "agent"


def test_resolution_batch_reuses_digest_and_metadata_reads(tmp_path: Path, monkeypatch):
    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "one.md"
    (threads / source).write_text("# Agent brief: x\n")
    event = {"kind": "ask", "text": "x", "source": source}
    calls = 0
    original = provenance.sha256_file

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(provenance, "sha256_file", counted)
    with provenance.resolution_batch(threads):
        assert provenance.resolve(event, threads) == "agent"
        assert provenance.resolve(event, threads) == "agent"
    assert calls == 1


def test_invalid_event_or_journal_author_fails_closed(tmp_path: Path):
    with pytest.warns(RuntimeWarning, match="invalid author"):
        assert provenance.resolve({"kind": "ask", "text": "x", "author": "bot"}) == "unknown"

    threads = tmp_path / "gc-threads"
    threads.mkdir()
    source = "one.md"
    path = threads / source
    path.write_text("legacy")
    digest = provenance.sha256_file(path)
    row = {"id": "a" * 32, "source": source, "sha256": digest,
           "author": "bot", "evidence": "bad", "timestamp": "now"}
    (tmp_path / "gc-provenance.jsonl").write_text(json.dumps(row) + "\n")
    with provenance.resolution_batch(threads):
        with pytest.warns(RuntimeWarning, match="invalid author"):
            assert provenance.resolve({"kind": "ask", "text": "x", "source": source}, threads) == "unknown"
