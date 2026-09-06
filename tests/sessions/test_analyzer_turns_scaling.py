"""Scaling and async-boundary regressions for transcript analysis."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn

import pytest

import gobby.sessions.analyzer_turns as analyzer_turns_module
import gobby.sessions.handoff_summary as handoff_summary_module
import gobby.sessions.summarize as summarize_module
from gobby.sessions.analyzer_turns import analyzer_turns_from_transcript
from gobby.sessions.transcripts.base import ParseEvent, RawLine
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.tool_activity import ToolActivityEntry

pytestmark = pytest.mark.unit


def _mcp_input(task_id: object) -> dict[str, Any]:
    return {
        "server_name": "gobby-tasks",
        "tool_name": "get_task",
        "arguments": {"task_id": task_id},
    }


def _mcp_call(call_id: str, task_id: object) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "mcp__gobby__call_tool",
            "arguments": json.dumps(_mcp_input(task_id)),
            "call_id": call_id,
        },
    }


def _mcp_item(task_id: object, *, failed: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "McpToolCall",
        "server": "gobby",
        "tool": "call_tool",
        "arguments": _mcp_input(task_id),
        "status": "failed" if failed else "completed",
        "result": {"Err": {"message": "lookup failed"}} if failed else {"success": True},
    }
    return {"payload": {"type": "item_completed", "item": item}}


def _user(text: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def test_codex_item_projection_preserves_turn_scoped_matching_and_native_details() -> None:
    repeated_id = "#same"
    turns = [
        _user("first turn"),
        _mcp_call("first-call", repeated_id),
        _mcp_call("repeated-call", repeated_id),
        _mcp_item(repeated_id),
        {
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "changes": {"src/changed.py": {"type": "update"}},
                    "status": "completed",
                },
            }
        },
        _user("second turn"),
        _mcp_call("second-call", repeated_id),
        _mcp_item(repeated_id, failed=True),
        {
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "command": ["git", "status", "--short"],
                    "exit_code": 0,
                    "stdout": "clean",
                },
            }
        },
    ]

    adapted = analyzer_turns_from_transcript(CodexTranscriptParser(), turns)
    blocks = [block for turn in adapted for block in turn["message"]["content"]]
    uses = [block for block in blocks if block["type"] == "tool_use"]
    results = [block for block in blocks if block["type"] == "tool_result"]
    use_ids = {block["id"] for block in uses}

    assert "first-call" not in use_ids
    assert "repeated-call" in use_ids
    assert "second-call" not in use_ids
    assert len([block for block in uses if block["name"] == "mcp gobby-tasks:get_task"]) == 3
    file_change = next(
        block
        for block in uses
        if block["name"] == "apply_patch" and block["input"] == {"file_path": "src/changed.py"}
    )
    assert not any(result["tool_use_id"] == file_change["id"] for result in results)
    assert any(block["name"] == "Bash" for block in uses)
    assert all(result["tool_use_id"] in use_ids for result in results)
    assert any(result["is_error"] and "lookup failed" in result["content"] for result in results)


def _scaling_transcript(size: int) -> list[dict[str, Any]]:
    turns = [_mcp_call(f"unowned-{index}", f"#unowned-{index}") for index in range(size)]
    turns.extend(_user(f"turn {index}") for index in range(size))
    turns.extend(_mcp_item(f"#unowned-{index}") for index in range(size))
    turns.extend(_mcp_call(f"owned-{index}", f"#owned-{index}") for index in range(size))
    turns.extend(_mcp_item(f"#owned-{index}") for index in reversed(range(size)))
    return turns


@pytest.mark.parametrize(
    ("call_value", "item_value", "matches"),
    [
        (1, 1.0, True),
        (True, 1, True),
        (0.0, -0.0, True),
        ({"nested": [1, {"flag": False}]}, {"nested": [1.0, {"flag": 0}]}, True),
        ({"a": 1, "b": 2}, {"b": 2.0, "a": True}, True),
        ("1", 1, False),
        ([], {}, False),
        ([1, 2], [2, 1], False),
    ],
)
def test_indexed_matching_preserves_json_equality_and_fifo(
    call_value: object, item_value: object, matches: bool
) -> None:
    adapted = analyzer_turns_from_transcript(
        CodexTranscriptParser(),
        [
            _user("match native items"),
            _mcp_call("first", call_value),
            _mcp_call("second", call_value),
            _mcp_item(item_value),
        ],
    )
    use_ids = [
        block["id"]
        for turn in adapted
        for block in turn["message"]["content"]
        if block["type"] == "tool_use"
    ]
    expected = ["second", "codex-item-3-0"] if matches else ["first", "second", "codex-item-3-0"]
    assert use_ids == expected


@pytest.mark.parametrize("size", [32, 128])
def test_turn_collection_and_exec_replacement_bound_comparisons(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    index_comparisons = 0
    call_comparisons = 0

    class CountingIndex(int):
        __hash__ = int.__hash__

        def __eq__(self, other: object) -> bool:
            nonlocal index_comparisons
            index_comparisons += 1
            return super().__eq__(other)

        def __ne__(self, other: object) -> bool:
            nonlocal index_comparisons
            index_comparisons += 1
            return super().__ne__(other)

    class CountingScan(CodexTranscriptParser):
        def iter_parse_events(
            self, raw_lines: Iterable[RawLine], start_index: int = 0
        ) -> Iterator[ParseEvent]:
            for event in super().iter_parse_events(raw_lines, start_index):
                event.raw_line_no = CountingIndex(event.raw_line_no)
                yield event

    original_equal = analyzer_turns_module._AdaptedCall.__eq__

    def counted_call_equal(self: analyzer_turns_module._AdaptedCall, other: object) -> bool:
        nonlocal call_comparisons
        call_comparisons += 1
        return original_equal(self, other)

    monkeypatch.setattr(analyzer_turns_module, "fresh_scan_parser", lambda parser: CountingScan())
    monkeypatch.setattr(analyzer_turns_module._AdaptedCall, "__eq__", counted_call_equal)
    turns: list[dict[str, Any]] = []
    for index in range(size):
        turns.extend(
            [
                _user(f"turn {index}"),
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": f"exec-{index}",
                        "name": "functions.exec",
                        "input": 'const r = await tools.exec_command({cmd:"git status"}); text(r);',
                    },
                },
            ]
        )
        # Stay inside the parser's bounded pending-call window while the retained
        # completed-call history grows across batches.
        if (index + 1) % 32 == 0:
            turns.extend(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": f"exec-{completed}",
                        "output": '{"exit_code":0,"output":"clean"}',
                    },
                }
                for completed in range(index, index - 32, -1)
            )

    adapted = analyzer_turns_from_transcript(CodexTranscriptParser(), turns)
    use_ids = {
        block["id"]
        for turn in adapted
        for block in turn["message"]["content"]
        if block["type"] == "tool_use"
    }

    assert use_ids == {f"exec-{index}:0" for index in range(size)}
    assert index_comparisons <= 4 * size
    assert call_comparisons <= 4 * size


def test_large_codex_reconstruction_bounds_owner_and_candidate_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_lookups = 0
    candidate_comparisons = 0
    original_owner = analyzer_turns_module._turn_owner
    original_matches = analyzer_turns_module._activity_matches

    def counted_owner(record_index: int, user_indexes: list[int]) -> int | None:
        nonlocal owner_lookups
        owner_lookups += 1
        return original_owner(record_index, user_indexes)

    def counted_match(
        item: ToolActivityEntry,
        call: analyzer_turns_module._AdaptedCall,
    ) -> bool:
        nonlocal candidate_comparisons
        candidate_comparisons += 1
        return original_matches(item, call)

    monkeypatch.setattr(analyzer_turns_module, "_turn_owner", counted_owner)
    monkeypatch.setattr(analyzer_turns_module, "_activity_matches", counted_match)

    size = 128
    analyzer_turns_from_transcript(CodexTranscriptParser(), _scaling_transcript(size))

    assert owner_lookups == 4 * size
    assert candidate_comparisons == size


def test_native_items_replace_shared_record_blocks_with_bounded_comparisons() -> None:
    comparisons = 0

    class CountingCommand(str):
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

    size = 128
    blocks: analyzer_turns_module._RecordBlocks = {}
    roles: dict[int, str] = {}
    calls: list[analyzer_turns_module._AdaptedCall] = []
    items: list[ToolActivityEntry] = []
    for index in range(size):
        tool_input = {"command": CountingCommand(f"echo {index}")}
        use = {"type": "tool_use", "input": tool_input, "id": f"call-{index}"}
        result = {"type": "tool_result", "tool_use_id": f"call-{index}"}
        analyzer_turns_module._append_block(blocks, roles, 0, "assistant", use)
        analyzer_turns_module._append_block(blocks, roles, 0, "user", result)
        calls.append(
            analyzer_turns_module._AdaptedCall(
                0, "Bash", tool_input, f"call-{index}", use, result, 0
            )
        )
        items.append(
            ToolActivityEntry(
                "Bash",
                {"command": f"echo {index}"},
                tool_use_id=f"native-{index}",
                record_index=index + 1,
            )
        )

    analyzer_turns_module._add_codex_items(list(reversed(items)), calls, [0], blocks, roles)

    assert not blocks[0]
    assert all(len(blocks[index + 1]) == 1 for index in range(size))
    assert comparisons <= 8 * size


@dataclass(frozen=True, slots=True)
class _TranscriptSession:
    transcript_path: str
    source: str = "codex"
    id: str = "session-id"
    machine_id: str = ""


class _AnalysisError(RuntimeError):
    pass


type _AnalysisRunner = Callable[[Path], Coroutine[Any, Any, object]]


async def _build_summary_source(path: Path) -> object:
    from gobby.utils.machine_id import get_machine_id

    machine_id = get_machine_id()
    assert machine_id is not None
    return await summarize_module.build_summary_source_context(
        _TranscriptSession(str(path), machine_id=machine_id),
        db=None,
        session_manager=SimpleNamespace(db=None),
        session_summary_config=None,
    )


async def _load_handoff_commits(path: Path) -> object:
    return await handoff_summary_module._load_transcript_commit_shas(_TranscriptSession(str(path)))


@pytest.fixture(
    params=[
        (summarize_module, _build_summary_source),
        (handoff_summary_module, _load_handoff_commits),
    ]
)
def analysis_path(request: pytest.FixtureRequest) -> tuple[ModuleType, _AnalysisRunner]:
    module, runner = request.param
    return module, runner


@pytest.mark.asyncio
async def test_transcript_analysis_paths_keep_work_off_loop_and_propagate_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    analysis_path: tuple[ModuleType, _AnalysisRunner],
) -> None:
    module, runner = analysis_path
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("", encoding="utf-8")
    main_thread = threading.get_ident()
    reconstruction_started = threading.Event()
    reconstruction_release = threading.Event()
    reconstruction_threads: list[int] = []
    analysis_threads: list[int] = []

    def blocking_reconstruction(
        parser: object, turns: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        reconstruction_threads.append(threading.get_ident())
        reconstruction_started.set()
        if not reconstruction_release.wait(timeout=2):
            raise AssertionError("transcript reconstruction blocked the event loop")
        return []

    class RaisingAnalyzer:
        def __init__(self, parser: object) -> None:
            self.parser = parser

        def extract_handoff_context(
            self, turns: list[dict[str, Any]], **kwargs: object
        ) -> NoReturn:
            analysis_threads.append(threading.get_ident())
            raise _AnalysisError("analysis failed")

    monkeypatch.setattr(module, "analyzer_turns_from_transcript", blocking_reconstruction)
    monkeypatch.setattr(module, "TranscriptAnalyzer", RaisingAnalyzer)

    pending: asyncio.Task[object] = asyncio.create_task(runner(transcript))
    while not reconstruction_started.is_set():
        if pending.done():
            await pending
        await asyncio.sleep(0)
    reconstruction_release.set()

    with pytest.raises(_AnalysisError, match="analysis failed"):
        await pending
    assert reconstruction_threads[0] != main_thread
    assert analysis_threads[0] != main_thread


@pytest.mark.asyncio
async def test_transcript_analysis_paths_propagate_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    analysis_path: tuple[ModuleType, _AnalysisRunner],
) -> None:
    module, runner = analysis_path
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("", encoding="utf-8")
    reconstruction_started = threading.Event()
    reconstruction_release = threading.Event()

    def blocking_reconstruction(
        parser: object, turns: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        reconstruction_started.set()
        if not reconstruction_release.wait(timeout=2):
            raise AssertionError("transcript reconstruction blocked the event loop")
        return []

    monkeypatch.setattr(module, "analyzer_turns_from_transcript", blocking_reconstruction)

    pending: asyncio.Task[object] = asyncio.create_task(runner(transcript))
    while not reconstruction_started.is_set():
        if pending.done():
            await pending
        await asyncio.sleep(0)
    pending.cancel()
    reconstruction_release.set()

    with pytest.raises(asyncio.CancelledError):
        await pending
