"""Scaling and async-boundary regressions for transcript analysis."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn

import pytest

import gobby.sessions.analyzer_turns as analyzer_turns_module
import gobby.sessions.handoff_summary as handoff_summary_module
import gobby.sessions.summarize as summarize_module
from gobby.sessions.analyzer_turns import analyzer_turns_from_transcript
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.tool_activity import ToolActivityEntry

pytestmark = pytest.mark.unit


def _mcp_input(task_id: str) -> dict[str, Any]:
    return {
        "server_name": "gobby-tasks",
        "tool_name": "get_task",
        "arguments": {"task_id": task_id},
    }


def _mcp_call(call_id: str, task_id: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "mcp__gobby__call_tool",
            "arguments": json.dumps(_mcp_input(task_id)),
            "call_id": call_id,
        },
    }


def _mcp_item(task_id: str, *, failed: bool = False) -> dict[str, Any]:
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
        if block["name"] == "apply_patch"
        and block["input"] == {"file_path": "src/changed.py"}
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


@dataclass(frozen=True, slots=True)
class _TranscriptSession:
    transcript_path: str
    source: str = "codex"
    id: str = "session-id"


class _AnalysisError(RuntimeError):
    pass


type _AnalysisRunner = Callable[[Path], Coroutine[Any, Any, object]]


async def _build_summary_source(path: Path) -> object:
    return await summarize_module.build_summary_source_context(
        _TranscriptSession(str(path)),
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
        await asyncio.sleep(0)
    pending.cancel()
    reconstruction_release.set()

    with pytest.raises(asyncio.CancelledError):
        await pending
