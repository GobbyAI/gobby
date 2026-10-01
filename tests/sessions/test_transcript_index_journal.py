"""Append persistence must scale with the new records, not transcript history."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from gobby.sessions import transcript_index as indexes
from gobby.sessions import transcript_index_resume as resumes
from gobby.sessions import transcript_index_sidecar as sidecars
from gobby.sessions.processor_stats import ProcessorStatsMixin
from gobby.sessions.transcript_index_resume import hydrate_appender_from_index

if TYPE_CHECKING:
    from gobby.sessions.processor_types import ProcessorHost

pytestmark = pytest.mark.unit


def _message(number: int) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "uuid": f"user-{number}",
                "timestamp": "2026-10-01T12:00:00Z",
                "message": {"role": "user", "content": f"message {number}"},
            }
        )
        + "\n"
    )


async def _read(path: Path) -> indexes.TranscriptIndex:
    st = path.stat()
    return await indexes.get_or_build_index(
        str(path), "claude", "journal-test", mtime_ns=st.st_mtime_ns, size=st.st_size
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [20, 2000])
async def test_resident_append_avoids_history_load_hash_and_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: int
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(_message(i) for i in range(history)))
    prior = await _read(path)
    prior_groups = prior.total_groups
    base = Path(sidecars._sidecar_path(str(path)))
    original_base = base.read_bytes()
    with path.open("a") as handle:
        handle.write(_message(history))

    with (
        patch.object(sidecars, "load_index_sidecar", wraps=sidecars.load_index_sidecar) as load,
        patch.object(
            sidecars, "_source_prefix_sha256", wraps=sidecars._source_prefix_sha256
        ) as hash_,
        patch.object(
            indexes, "build_index_from_file", wraps=indexes.build_index_from_file
        ) as build,
    ):
        grown = await _read(path)
    assert grown.raw_record_count == history + 1
    assert grown.total_groups == prior_groups + 1
    assert load.call_count == 0, "resident append loaded historical sidecar"
    assert hash_.call_count == 0, "resident append hashed historical transcript"
    assert build.call_count == 0
    assert base.read_bytes() == original_base, "append rewrote the base sidecar"
    assert Path(str(base) + ".journal").stat().st_size < 3500

    indexes.clear_index_cache()
    with patch.object(
        indexes, "build_index_from_file", wraps=indexes.build_index_from_file
    ) as build:
        replayed = await _read(path)
    assert build.call_count == 0, "cold journal replay reparsed transcript"
    assert asdict(replayed) == asdict(grown)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["inode", "shrink", "head", "boundary"])
@pytest.mark.parametrize("resident", [True, False])
async def test_resident_index_fails_closed_on_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, resident: bool
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(_message(i) for i in range(200)))
    await _read(path)
    if not resident:
        indexes.clear_index_cache()
    original = path.read_bytes()
    st = path.stat()
    if change == "inode":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(original)
        os.replace(replacement, path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    elif change == "shrink":
        path.write_bytes(original[: original.find(b"\n") + 1])
    else:
        offset = 0 if change == "head" else original.rfind(b"message")
        rewritten = bytearray(original)
        rewritten[offset] = ord(" ") if rewritten[offset] != ord(" ") else ord("x")
        path.write_bytes(rewritten)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with patch.object(
        indexes, "build_index_from_file", wraps=indexes.build_index_from_file
    ) as build:
        await _read(path)
    assert build.call_count == 1, f"resident index accepted changed {change}"


@pytest.mark.asyncio
async def test_processor_eof_saves_journal_and_clone_rollback_stays_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text(_message(0))
    prior = await _read(path)
    appender = hydrate_appender_from_index(
        indexes.TranscriptIndexAppender("claude", "journal-test", str(path)), prior
    )
    base = Path(sidecars._sidecar_path(str(path)))
    original_base = base.read_bytes()
    original_index = asdict(prior)
    for number in range(1, 4):
        tail = _message(number)
        offset = path.stat().st_size
        with path.open("a") as handle:
            handle.write(tail)
        st = path.stat()
        candidate = appender.clone()
        candidate.append_positioned_lines(
            [tail], [offset], mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        assert appender.index.raw_record_count == number, "speculative append reached survivor"
        await ProcessorStatsMixin._persist_appender_snapshot(
            cast("ProcessorHost", object()), "journal-test", str(path), candidate, st
        )
        appender = candidate
    assert asdict(prior) == original_index
    assert base.read_bytes() == original_base
    indexes.clear_index_cache()
    replayed = await _read(path)
    st = path.stat()
    fresh = indexes.build_index_from_file(
        str(path), "claude", "journal-test", mtime_ns=st.st_mtime_ns, size=st.st_size
    )
    assert asdict(replayed) == asdict(fresh)


@pytest.mark.asyncio
async def test_partial_journal_fails_closed_and_full_build_compacts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text(_message(0))
    await _read(path)
    journal = Path(sidecars._sidecar_path(str(path)) + ".journal")
    with journal.open("ab") as handle:
        handle.write(b'{"generation":')
    indexes.clear_index_cache()
    with patch.object(
        indexes, "build_index_from_file", wraps=indexes.build_index_from_file
    ) as build:
        rebuilt = await _read(path)
    assert build.call_count == 1
    assert rebuilt.raw_record_count == 1
    assert journal.read_bytes() == b""


@pytest.mark.asyncio
async def test_processor_catches_up_after_reader_advances_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text(_message(0))
    await _read(path)
    # The processor reconstructs its own appender from disk, independently of
    # the reader's resident containers.
    st = path.stat()
    processor_index = sidecars.load_index_sidecar(
        str(path),
        "claude",
        "journal-test",
        seek_mode="byte",
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
    )
    assert processor_index is not None
    appender = hydrate_appender_from_index(
        indexes.TranscriptIndexAppender("claude", "journal-test", str(path)), processor_index
    )
    candidate = appender.clone()
    offset = path.stat().st_size
    with path.open("a") as handle:
        handle.write(_message(1))
    await _read(path)
    second_offset = path.stat().st_size
    with path.open("a") as handle:
        handle.write(_message(2))
    st = path.stat()
    candidate.append_positioned_lines(
        [_message(1), _message(2)],
        [offset, second_offset],
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
    )
    await ProcessorStatsMixin._persist_appender_snapshot(
        cast("ProcessorHost", object()), "journal-test", str(path), candidate, st
    )
    indexes.clear_index_cache()
    with patch.object(
        resumes, "extend_index_from_file", wraps=resumes.extend_index_from_file
    ) as extend:
        replayed = await _read(path)
    assert extend.call_count == 0, "processor stopped persisting after a competing reader write"
    assert asdict(replayed) == asdict(candidate.index)


@pytest.mark.asyncio
async def test_failed_resident_append_rolls_back_the_speculative_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text(_message(0))
    prior = await _read(path)
    original = asdict(prior)
    with path.open("a") as handle:
        handle.write(_message(1))
    with patch.object(
        indexes.TranscriptIndexAppender, "snapshot", side_effect=RuntimeError("failed batch")
    ):
        with pytest.raises(RuntimeError, match="failed batch"):
            await _read(path)
    assert asdict(prior) == original
    recovered = await _read(path)
    assert recovered.raw_record_count == 2
    assert recovered.total_groups == 2


@pytest.mark.asyncio
async def test_same_size_middle_rewrite_is_deferred_until_full_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(_message(i) for i in range(200)))
    prior = await _read(path)
    st = path.stat()
    lines = path.read_text().splitlines(keepends=True)
    lines[100] = lines[100].replace("T12:00:00Z", "T11:00:00Z")
    path.write_text("".join(lines))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with patch.object(
        indexes, "build_index_from_file", wraps=indexes.build_index_from_file
    ) as build:
        cached = await _read(path)
        assert build.call_count == 0
        assert cached.boundaries[100].timestamp.hour == prior.boundaries[100].timestamp.hour == 12
        indexes.discard_index_sidecar(str(path))
        rebuilt = await _read(path)
        assert build.call_count == 1
    assert rebuilt.boundaries[100].timestamp.hour == 11


@pytest.mark.asyncio
async def test_queued_reader_keeps_the_transcript_lock_until_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    indexes.clear_index_cache()
    path = tmp_path / "transcript.jsonl"
    path.write_text(_message(0))
    await _read(path)
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event(), threading.Event()]
    counter_lock = threading.Lock()
    calls = active = maximum_active = 0
    original = sidecars._resident_matches

    def gated_match(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls, active, maximum_active
        with counter_lock:
            number = calls
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
        entered[number].set()
        try:
            if number < 2:
                release[number].wait(5)
            return original(*args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(sidecars, "_resident_matches", gated_match)
    first = asyncio.create_task(_read(path))
    assert await asyncio.to_thread(entered[0].wait, 5)
    queued = asyncio.Event()

    async def queued_read() -> indexes.TranscriptIndex:
        queued.set()
        return await _read(path)

    second = asyncio.create_task(queued_read())
    await queued.wait()
    release[0].set()
    await first
    assert await asyncio.to_thread(entered[1].wait, 5)
    third = asyncio.create_task(_read(path))
    try:
        await asyncio.to_thread(entered[2].wait, 1)
    finally:
        release[1].set()
        await asyncio.gather(second, third)
    assert maximum_active == 1, "a fresh reader bypassed the queued reader's transcript lock"
