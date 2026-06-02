"""Tests for the transcript boundary index (windowed-render foundation).

Asserts the index built through the real ``render_incremental`` path agrees with
a full ``render_transcript`` (group count, group-start global indices), captures
duplicate-suppression and post-pass data, and that the bounded async cache
builds once and invalidates on append.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import gobby.sessions.transcript_index as transcript_index
from gobby.sessions.transcript_index import (
    INDEX_CACHE_MAX_ENTRIES,
    TranscriptIndexAppender,
    build_index_from_file,
    clear_index_cache,
    detect_source_bounded,
    get_or_build_index,
    load_index_sidecar,
    persist_index_sidecar,
)
from gobby.sessions.transcript_io import _count_nonempty_lines
from gobby.sessions.transcript_renderer import RenderedMessage, render_transcript
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser

SESSION = "s1"


def _codex_lines() -> list[str]:
    def msg(role: str, text: str) -> str:
        block = "output_text" if role == "assistant" else "input_text"
        return json.dumps(
            {
                "timestamp": "2024-06-15T10:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": block, "text": text}],
                },
            }
        )

    def fn_call(name: str, call_id: str) -> str:
        return json.dumps(
            {
                "timestamp": "2024-06-15T10:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": name,
                    "arguments": "{}",
                    "call_id": call_id,
                },
            }
        )

    def fn_out(call_id: str, output: str) -> str:
        return json.dumps(
            {
                "timestamp": "2024-06-15T10:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )

    return [
        msg("user", "hello"),
        msg("assistant", "let me look"),
        fn_call("read", "call_1"),
        fn_out("call_1", "file contents"),
        msg("assistant", "done"),
        msg("user", "thanks"),
        msg("assistant", "you are welcome"),
    ]


def _claude_lines() -> list[str]:
    def user(text: str) -> str:
        return json.dumps(
            {"type": "user", "message": {"content": text}, "timestamp": "2024-01-01T12:00:00Z"}
        )

    def assistant_multi() -> str:
        return json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me read that file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_read1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/test.txt"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_read2",
                            "name": "Grep",
                            "input": {"pattern": "foo"},
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

    def user_results() -> str:
        return json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_read1",
                            "content": "file contents here",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_read2",
                            "content": "second result",
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

    return [user("hi"), assistant_multi(), user_results(), user("bye")]


def _gemini_lines() -> list[str]:
    def line(role: str, text: str) -> str:
        return json.dumps({"type": role, "content": text, "timestamp": "2024-01-01T12:00:00Z"})

    return [line("user", "hi"), line("assistant", "hello"), line("user", "more")]


PARSERS: dict[str, tuple[type[Any], Callable[[], list[str]], str]] = {
    "codex": (CodexTranscriptParser, _codex_lines, "codex"),
    "claude": (ClaudeTranscriptParser, _claude_lines, "claude"),
    "gemini": (GeminiTranscriptParser, _gemini_lines, "gemini"),
}


def _write(tmp_path: Path, name: str, lines: list[str]) -> str:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _line_texts(lines: list[str]) -> list[str]:
    return [f"{line}\n" for line in lines]


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    offset = 0
    for line in _line_texts(lines):
        offsets.append(offset)
        offset += len(line.encode("utf-8"))
    return offsets


def _group_start_index(group_id: str) -> int:
    """Global parsed index of the message that opened a rendered group."""
    return int(group_id.rsplit("-", 1)[-1])


def _full_groups(
    parser_cls: type[Any], lines: list[str]
) -> tuple[list[RenderedMessage], list[ParsedMessage]]:
    parsed = parser_cls(session_id=SESSION).parse_lines(lines)
    msgs = [m for m in parsed if isinstance(m, ParsedMessage)]
    return render_transcript(msgs, session_id=SESSION), msgs


@pytest.mark.parametrize("name", list(PARSERS))
def test_index_boundaries_match_full_render(tmp_path: Path, name: str) -> None:
    parser_cls, lines_fn, source = PARSERS[name]
    lines = lines_fn()
    path = _write(tmp_path, name, lines)
    st = os.stat(path)

    index = build_index_from_file(path, source, SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)
    full_groups, msgs = _full_groups(parser_cls, lines)

    assert index.total_groups == len(full_groups)
    assert index.parsed_message_count == len(msgs)
    assert index.raw_record_count == _count_nonempty_lines(lines)
    assert index.seek_mode == "byte"

    for ordinal, (boundary, group) in enumerate(zip(index.boundaries, full_groups, strict=True)):
        assert boundary.group_index == ordinal
        assert boundary.parsed_index_start == _group_start_index(group.id)


def test_byte_offsets_seek_to_group_start(tmp_path: Path) -> None:
    """Each boundary's byte_start points at the raw line that opens the group."""
    lines = _codex_lines()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    with open(path, "rb") as handle:
        for boundary in index.boundaries:
            assert boundary.byte_start is not None
            handle.seek(boundary.byte_start)
            raw = handle.readline().decode("utf-8")
            record = json.loads(raw)
            # The line at byte_start is a real transcript record (not mid-line).
            assert isinstance(record, dict)
            assert record.get("type") == "response_item"


def test_tool_first_open_captures_tool_use(tmp_path: Path) -> None:
    lines = _codex_lines()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    parsed = [
        m
        for m in CodexTranscriptParser(session_id=SESSION).parse_lines(lines)
        if isinstance(m, ParsedMessage)
    ]
    tool_use = next(m for m in parsed if m.tool_use_id == "call_1" and m.content_type == "tool_use")

    assert index.tool_first_open == {"call_1": tool_use.index}


def test_multimessage_line_second_group_not_resume_safe(tmp_path: Path) -> None:
    """A user line that expands to text + orphan tool_result opens two groups.

    The second group starts at the *2nd* parsed message of the same event, so its
    boundary must be marked ``resume_safe=False`` (seeking there would re-parse
    the line from its first message and mis-assign global indices).
    """
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "hello there"},
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "x"},
                ]
            },
            "timestamp": "2024-01-01T12:00:00Z",
        }
    )
    path = _write(tmp_path, "claude", [line])
    st = os.stat(path)
    index = build_index_from_file(path, "claude", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full_groups, _ = _full_groups(ClaudeTranscriptParser, [line])
    assert index.total_groups == len(full_groups) == 2

    first, second = index.boundaries
    assert first.resume_safe is True
    assert first.parsed_index_start == 0
    # Both expanded messages share one raw line; the second group opens mid-event.
    assert second.parsed_index_start == 1
    assert second.raw_line_start == first.raw_line_start
    assert second.resume_safe is False


def test_droid_sidecar_usage_is_post_pass_adjustment(tmp_path: Path) -> None:
    transcript = tmp_path / "droid-abc.jsonl"
    settings = tmp_path / "droid-abc.settings.json"
    lines = [
        json.dumps({"type": "session_start", "version": 2}),
        json.dumps(
            {
                "type": "message",
                "timestamp": "2024-01-01T12:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            }
        ),
        json.dumps(
            {
                "type": "message",
                "timestamp": "2024-01-01T12:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings.write_text(
        json.dumps({"model": "claude", "tokenUsage": {"inputTokens": 5, "outputTokens": 7}}),
        encoding="utf-8",
    )
    st = os.stat(transcript)

    index = build_index_from_file(
        str(transcript), "droid", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size
    )

    assert len(index.post_pass_adjustments) == 1
    adjustment = index.post_pass_adjustments[0]
    assert adjustment.field == "usage"
    assert adjustment.value.input_tokens == 5
    assert adjustment.value.output_tokens == 7
    # The last assistant message renders into the final group.
    assert adjustment.group_index == index.total_groups - 1

    persist_index_sidecar(str(transcript), index)
    loaded = load_index_sidecar(
        str(transcript), "droid", seek_mode="byte", mtime_ns=st.st_mtime_ns, size=st.st_size
    )

    assert loaded is not None
    loaded_adjustment = loaded.post_pass_adjustments[0]
    assert loaded_adjustment.field == "usage"
    assert loaded_adjustment.value.input_tokens == 5
    assert loaded_adjustment.value.output_tokens == 7


def test_detect_source_bounded_prefers_path(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex" / "sessions"
    codex_dir.mkdir(parents=True)
    path = codex_dir / "rollout-x.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert detect_source_bounded(str(path)) == "codex"


def test_detect_source_bounded_samples_content(tmp_path: Path) -> None:
    path = tmp_path / "unknown.log"
    path.write_text("\n".join(_codex_lines()) + "\n", encoding="utf-8")
    assert detect_source_bounded(str(path), session_source="claude") == "codex"


@pytest.mark.asyncio
async def test_get_or_build_index_caches_and_invalidates(tmp_path: Path) -> None:
    clear_index_cache()
    lines = _codex_lines()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)

    first = await get_or_build_index(
        path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size
    )
    second = await get_or_build_index(
        path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size
    )
    assert first is second  # cache hit returns the same object

    # Append a record -> new size/mtime -> fresh build.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2024-06-15T10:30:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "again"}],
                    },
                }
            )
            + "\n"
        )
    st2 = os.stat(path)
    third = await get_or_build_index(
        path, "codex", SESSION, mtime_ns=st2.st_mtime_ns, size=st2.st_size
    )
    assert third is not first
    assert third.total_groups >= first.total_groups
    clear_index_cache()


@pytest.mark.asyncio
async def test_get_or_build_index_loads_valid_sidecar_after_cache_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_index_cache()
    path = _write(tmp_path, "codex-sidecar", _codex_lines())
    st = os.stat(path)

    first = await get_or_build_index(
        path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size
    )
    clear_index_cache()

    def fail_build(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("sidecar load should avoid a full rebuild")

    monkeypatch.setattr(transcript_index, "build_index_from_file", fail_build)
    loaded = await get_or_build_index(
        path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size
    )

    assert loaded is not first
    assert loaded.total_groups == first.total_groups
    assert loaded.parsed_message_count == first.parsed_message_count
    assert loaded.parsed_boundaries
    clear_index_cache()


@pytest.mark.asyncio
async def test_get_or_build_index_rebuilds_on_sidecar_size_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_index_cache()
    path = _write(tmp_path, "codex-truncated", _codex_lines())
    st = os.stat(path)
    await get_or_build_index(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)
    clear_index_cache()

    Path(path).write_text(_line_texts(_codex_lines()[0:2])[0], encoding="utf-8")
    st2 = os.stat(path)
    calls = 0
    original = transcript_index.build_index_from_file

    def count_build(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(transcript_index, "build_index_from_file", count_build)
    rebuilt = await get_or_build_index(
        path, "codex", SESSION, mtime_ns=st2.st_mtime_ns, size=st2.st_size
    )

    assert calls == 1
    assert rebuilt.size == st2.st_size
    clear_index_cache()


def test_transcript_index_appender_persists_append_growth(tmp_path: Path) -> None:
    lines = _codex_lines()
    path = _write(tmp_path, "codex-append", lines)
    offsets = _line_offsets(lines)
    appender = TranscriptIndexAppender("codex", SESSION, path)
    first_batch = _line_texts(lines[:2])
    second_batch = _line_texts(lines[2:])

    appender.append_positioned_lines(
        first_batch,
        offsets[:2],
        mtime_ns=0,
        size=sum(len(line.encode("utf-8")) for line in first_batch),
    )
    first_count = appender.index.parsed_message_count
    st = os.stat(path)
    appender.append_positioned_lines(
        second_batch,
        offsets[2:],
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
    )
    persist_index_sidecar(path, appender.snapshot(mtime_ns=st.st_mtime_ns, size=st.st_size))
    loaded = load_index_sidecar(
        path, "codex", seek_mode="byte", mtime_ns=st.st_mtime_ns, size=st.st_size
    )
    rebuilt = build_index_from_file(
        path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size
    )

    assert loaded is not None
    assert loaded.parsed_message_count > first_count
    assert loaded.parsed_message_count == rebuilt.parsed_message_count
    assert loaded.total_groups == rebuilt.total_groups
    assert loaded.parsed_boundaries


@pytest.mark.asyncio
async def test_index_cache_evicts_beyond_capacity(tmp_path: Path) -> None:
    clear_index_cache()
    # Build more distinct snapshots than the cache holds; the first must evict.
    paths = []
    for i in range(INDEX_CACHE_MAX_ENTRIES + 3):
        path = _write(tmp_path, f"codex-{i}", _codex_lines())
        st = os.stat(path)
        await get_or_build_index(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)
        paths.append((path, st))

    from gobby.sessions.transcript_index import _INDEX_CACHE

    assert len(_INDEX_CACHE) == INDEX_CACHE_MAX_ENTRIES
    clear_index_cache()
