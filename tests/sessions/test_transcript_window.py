"""Tests for windowed transcript rendering (:mod:`gobby.sessions.transcript_window`).

The load-bearing invariant: concatenating every ``render_window`` page (cap
disabled) reproduces ``render_transcript`` **exactly** — same ``RenderedMessage``
ids, grouping, ``source_line``, cross-group/duplicate tool-result suppression, the
EOF group, and a non-``resume_safe`` group reconstructed from its preceding
``resume_safe`` boundary. A separate low-cap suite pins the degraded wire shape and
``returned_count``-based paging composition.
"""

from __future__ import annotations

import json
import os

import pytest

from gobby.sessions.transcript_index import build_index_from_file
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcript_window import (
    MAX_WINDOW_SPAN_BYTES,
    render_window,
)
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser

SESSION = "win-s1"
HUGE = 1 << 30  # disable the span cap for exact equivalence


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _codex_msg(role: str, text: str) -> str:
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


def _codex_call(name: str, call_id: str) -> str:
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


def _codex_out(call_id: str, output: str) -> str:
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


def _codex_rich() -> list[str]:
    """Exercises adjacent + non-adjacent (cross-group) results, an unresolved-at-EOF
    call, and an EOF flush group. (A *distant* duplicate for an already-resolved
    in-window call is deliberately excluded — full render's last-write-wins reaches
    back across the whole stream, which bounded windowing cannot reproduce without
    reading to EOF; the realistic post-cut duplicate is covered separately.)"""
    return [
        _codex_msg("user", "q1"),
        _codex_msg("assistant", "thinking"),
        _codex_call("read", "c1"),
        _codex_out("c1", "result-1"),  # adjacent resolution
        _codex_msg("assistant", "after-read"),
        _codex_msg("user", "q2"),
        _codex_msg("assistant", "a2"),
        _codex_call("grep", "c2"),  # resolved much later (non-adjacent)
        _codex_msg("user", "q3"),
        _codex_msg("assistant", "a3"),
        _codex_out("c2", "late-result"),
        _codex_msg("assistant", "a4"),
        _codex_call("bash", "c3"),  # never resolved -> pending at EOF
        _codex_msg("assistant", "final"),  # EOF flush group
    ]


def _codex_postcut_duplicate() -> list[str]:
    """A call resolved in group 1, then a *duplicate* result for it lands in a
    later group's span — the post-cut duplicate the plan's suppression invariant
    targets (call first opened before the window start)."""
    return [
        _codex_msg("user", "q1"),
        _codex_msg("assistant", "open"),
        _codex_call("read", "dup"),  # opened + resolved in group 1
        _codex_out("dup", "first-result"),
        _codex_msg("user", "q2"),
        _codex_msg("assistant", "a2"),
        _codex_out("dup", "second-result"),  # duplicate; call opened before any later window
        _codex_msg("user", "q3"),
        _codex_msg("assistant", "a3"),
    ]


def _claude_multimessage() -> list[str]:
    """A user line that expands to text + orphan tool_result (2 groups; the 2nd
    boundary is *not* resume_safe), plus a normal tool pairing across groups."""

    def user(text: str) -> str:
        return json.dumps(
            {"type": "user", "message": {"content": text}, "timestamp": "2024-01-01T12:00:00Z"}
        )

    def assistant_tool() -> str:
        return json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {"type": "text", "text": "Reading the file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_a",
                            "name": "Read",
                            "input": {"file_path": "/tmp/x"},
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:01Z",
            }
        )

    def tool_result() -> str:
        return json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "file body"}
                    ]
                },
                "timestamp": "2024-01-01T12:00:02Z",
            }
        )

    def text_plus_orphan() -> str:
        return json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "and another thing"},
                        {"type": "tool_result", "tool_use_id": "orphan", "content": "z"},
                    ]
                },
                "timestamp": "2024-01-01T12:00:03Z",
            }
        )

    return [user("hi"), assistant_tool(), tool_result(), text_plus_orphan(), user("bye")]


FIXTURES = {
    "codex": (CodexTranscriptParser, _codex_rich, "codex"),
    "claude": (ClaudeTranscriptParser, _claude_multimessage, "claude"),
}


def _write(tmp_path, name: str, lines: list[str]) -> str:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _full_render(parser_cls, lines: list[str]):
    parsed = [
        m for m in parser_cls(session_id=SESSION).parse_lines(lines) if isinstance(m, ParsedMessage)
    ]
    return render_transcript(parsed, session_id=SESSION)


def _assert_equiv(full, windowed) -> None:
    assert len(windowed) == len(full)
    for expected, actual in zip(full, windowed, strict=True):
        assert actual.to_dict() == expected.to_dict()


def _page_head(path: str, source: str, index, limit: int):
    out = []
    offset = 0
    while offset < index.total_groups:
        result = render_window(
            path, source, SESSION, index, limit=limit, offset=offset, order="head", max_span=HUGE
        )
        assert result.returned_count >= 1
        assert result.returned_count <= limit
        out.extend(result.groups)
        offset += result.returned_count
    return out


def _page_tail(path: str, source: str, index, limit: int):
    out: list = []
    offset = 0
    while offset < index.total_groups:
        result = render_window(
            path, source, SESSION, index, limit=limit, offset=offset, order="tail", max_span=HUGE
        )
        assert result.returned_count >= 1
        assert result.returned_count <= limit
        out = list(result.groups) + out  # prepend older pages
        offset += result.returned_count
    return out


# --------------------------------------------------------------------------- #
# Equivalence: concatenated windows == full render (cap disabled)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", list(FIXTURES))
def test_single_full_window_equals_full_render(tmp_path, name: str) -> None:
    parser_cls, lines_fn, source = FIXTURES[name]
    lines = lines_fn()
    path = _write(tmp_path, name, lines)
    st = os.stat(path)
    index = build_index_from_file(path, source, SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full = _full_render(parser_cls, lines)
    result = render_window(
        path,
        source,
        SESSION,
        index,
        limit=index.total_groups,
        offset=0,
        order="head",
        max_span=HUGE,
    )
    assert result.degraded is False
    _assert_equiv(full, result.groups)


@pytest.mark.parametrize("name", list(FIXTURES))
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_paged_head_windows_concatenate_to_full_render(tmp_path, name: str, limit: int) -> None:
    parser_cls, lines_fn, source = FIXTURES[name]
    lines = lines_fn()
    path = _write(tmp_path, name, lines)
    st = os.stat(path)
    index = build_index_from_file(path, source, SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full = _full_render(parser_cls, lines)
    _assert_equiv(full, _page_head(path, source, index, limit))


@pytest.mark.parametrize("name", list(FIXTURES))
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_paged_tail_windows_concatenate_to_full_render(tmp_path, name: str, limit: int) -> None:
    parser_cls, lines_fn, source = FIXTURES[name]
    lines = lines_fn()
    path = _write(tmp_path, name, lines)
    st = os.stat(path)
    index = build_index_from_file(path, source, SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full = _full_render(parser_cls, lines)
    _assert_equiv(full, _page_tail(path, source, index, limit))


def test_non_resume_safe_group_reconstructed(tmp_path) -> None:
    """The orphan-tool_result group (2nd parsed message of one raw line) is not a
    valid seek target; windowing it must resume from the preceding resume_safe
    boundary and reproduce it with the correct global index."""
    lines = _claude_multimessage()
    path = _write(tmp_path, "claude", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "claude", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    # Find the non-resume_safe boundary and window exactly that one group.
    target = next(b for b in index.boundaries if not b.resume_safe)
    result = render_window(
        path,
        "claude",
        SESSION,
        index,
        limit=1,
        offset=target.group_index,
        order="head",
        max_span=HUGE,
    )
    full = _full_render(ClaudeTranscriptParser, lines)
    assert result.returned_count == 1
    assert result.groups[0].to_dict() == full[target.group_index].to_dict()


def test_postcut_duplicate_result_suppressed(tmp_path) -> None:
    """A window starting after a call's open must seed a stub so a duplicate
    tool_result for that pre-window call is absorbed (no orphan group), matching
    the full render's same region."""
    lines = _codex_postcut_duplicate()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full = _full_render(CodexTranscriptParser, lines)
    # Window strictly after group 1 (where "dup" was opened); the duplicate
    # result for "dup" falls inside this region and must not become an orphan.
    result = render_window(
        path, "codex", SESSION, index, limit=10, offset=2, order="head", max_span=HUGE
    )
    assert result.returned_count == index.total_groups - 2
    _assert_equiv(full[2:], result.groups)


def test_tail_offset_zero_is_newest_slice(tmp_path) -> None:
    lines = _codex_rich()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full = _full_render(CodexTranscriptParser, lines)
    result = render_window(
        path, "codex", SESSION, index, limit=2, offset=0, order="tail", max_span=HUGE
    )
    assert result.returned_count == 2
    _assert_equiv(full[-2:], result.groups)


# --------------------------------------------------------------------------- #
# Degraded path + returned_count paging composition (cap low)
# --------------------------------------------------------------------------- #


def _degraded_lines() -> list[str]:
    lines = [
        _codex_msg("user", "start"),
        _codex_msg("assistant", "calling tool"),
        _codex_call("bash", "d1"),  # opened in the head window, resolved far away
    ]
    # User fillers force a fresh group each (assistant messages would merge into
    # the d1 group and never close it), inflating the span past the cap.
    lines += [_codex_msg("user", f"filler chunk number {i} " + "x" * 80) for i in range(20)]
    lines.append(_codex_out("d1", "the result arrives only after the budget is gone"))
    return lines


def test_forward_extension_budget_marks_degraded(tmp_path) -> None:
    lines = _degraded_lines()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    # Window the assistant group that opens d1; force forward extension to run out
    # of budget before d1's far-away result.
    result = render_window(
        path, "codex", SESSION, index, limit=1, offset=1, order="head", max_span=400
    )
    assert result.degraded is True
    assert result.degraded_reason == "max_span_exceeded"

    pending = [
        tc
        for group in result.groups
        for block in group.content_blocks
        if block.type == "tool_chain" and block.tool_calls
        for tc in block.tool_calls
        if tc.id == "d1"
    ]
    assert len(pending) == 1
    assert pending[0].status == "pending"
    assert pending[0].result is None


@pytest.mark.parametrize("order", ["head", "tail"])
def test_returned_count_paging_tiles_without_gaps(tmp_path, order: str) -> None:
    """Under a low cap, pages shrink but advancing offset by returned_count must
    tile [0, total) exactly — no gaps, no overlaps — for both orders."""
    lines = _codex_rich()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)
    total = index.total_groups

    covered: list[tuple[int, int]] = []
    offset = 0
    guard = 0
    while offset < total:
        guard += 1
        assert guard <= total + 5, "paging failed to make progress"
        result = render_window(
            path, "codex", SESSION, index, limit=4, offset=offset, order=order, max_span=300
        )
        assert 1 <= result.returned_count <= 4
        if order == "tail":
            covered.append((total - offset - result.returned_count, total - offset))
        else:
            covered.append((offset, offset + result.returned_count))
        offset += result.returned_count

    covered.sort()
    assert covered[0][0] == 0
    assert covered[-1][1] == total
    for (_prev_start, prev_end), (next_start, _next_end) in zip(covered, covered[1:], strict=False):
        assert prev_end == next_start  # contiguous, no gap/overlap


def test_default_max_span_is_unbounded_for_small_transcripts(tmp_path) -> None:
    """A normal small transcript renders exactly under the production cap."""
    lines = _codex_rich()
    path = _write(tmp_path, "codex", lines)
    st = os.stat(path)
    index = build_index_from_file(path, "codex", SESSION, mtime_ns=st.st_mtime_ns, size=st.st_size)

    full = _full_render(CodexTranscriptParser, lines)
    result = render_window(
        path,
        "codex",
        SESSION,
        index,
        limit=index.total_groups,
        offset=0,
        order="head",
        max_span=MAX_WINDOW_SPAN_BYTES,
    )
    assert result.degraded is False
    _assert_equiv(full, result.groups)
