"""Tests for transcript-derived task-close evidence."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.storage.session_models import Session
from gobby.tasks import transcript_outcomes
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import (
    WINDOW_LOOKBACK,
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptValidationRun,
    TranscriptValidationSegment,
    _resolve_transcript_path,
    derive_transcript_evidence,
    merge_transcript_evidence,
    select_window_raw_lines,
)
from gobby.tasks.transcript_outcomes import EvidenceOutcome
from gobby.tasks.transcript_outcomes import extract_output as _extract_output

BASE_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


def test_validation_output_is_bounded_with_failure_edges_preserved() -> None:
    output, truncated = _extract_output(
        {"output": "AssertionError: first\n" + ("x" * 20_000) + "\nImportError: last"}
    )

    assert truncated is True
    assert output is not None
    assert output.startswith("AssertionError: first")
    assert output.endswith("ImportError: last")
    assert len(output) <= 16_000


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


def _session(source: str, transcript_path: Path | None, *, suffix: str = "1") -> Session:
    return Session(
        id=f"00000000-0000-0000-0000-00000000000{suffix}",
        external_id=f"transcript-evidence-{source}-{suffix}",
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript_path) if transcript_path else None,
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _claude_tool_pair(
    *,
    command: str,
    call_id: str,
    start: datetime,
    result: Any,
    is_error: bool = False,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant",
            "timestamp": start.isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": (start + timedelta(seconds=1)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": result,
                        "is_error": is_error,
                    }
                ],
            },
        },
    ]


@pytest.mark.asyncio
async def test_claude_pairs_shell_results_and_tracks_task_edits(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    records = [
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_example.py",
            call_id="test-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "passed"},
        ),
        {
            "type": "assistant",
            "timestamp": (BASE_TIME + timedelta(seconds=2)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "edit-1",
                        "name": "Edit",
                        "input": {"file_path": str(tmp_path / "src" / "changed.py")},
                    }
                ],
            },
        },
    ]
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code, run.categories) for run in evidence.validation_runs] == [
        ("success", 0, ("test",))
    ]
    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [("src/changed.py", "Edit")]
    assert evidence.validation_runs[0].completed_at < evidence.edits[0].timestamp


@pytest.mark.asyncio
async def test_claim_window_excludes_earlier_validation_runs(tmp_path: Path) -> None:
    transcript = tmp_path / "window.jsonl"
    _write_jsonl(
        transcript,
        [
            *_claude_tool_pair(
                command="pytest tests/old.py",
                call_id="old",
                start=BASE_TIME,
                result="passed",
            ),
            *_claude_tool_pair(
                command="pytest tests/new.py",
                call_id="new",
                start=BASE_TIME + timedelta(minutes=2),
                result="passed",
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME + timedelta(minutes=1),
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.command for run in evidence.validation_runs] == ["pytest tests/new.py"]


def _codex_response_item(payload: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    return {"type": "response_item", "timestamp": timestamp.isoformat(), "payload": payload}


def _codex_direct_exec_pair(
    *,
    command: str,
    result: Any,
    call_id: str = "direct-1",
) -> list[dict[str, Any]]:
    return [
        _codex_response_item(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command}),
            },
            BASE_TIME,
        ),
        _codex_response_item(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
            BASE_TIME + timedelta(seconds=1),
        ),
    ]


@pytest.mark.asyncio
async def test_codex_consumes_nested_exec_outcome_and_apply_patch_edit(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    patch = "*** Begin Patch\n*** Update File: src/changed.py\n@@\n-old\n+new\n*** End Patch\n"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        'const r = await tools.exec_command({cmd:"uv run pytest tests/tasks"}); '
                        "text(r);"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": json.dumps({"exit_code": 0, "output": "passed"}),
                },
                BASE_TIME + timedelta(seconds=1),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "patch-1",
                    "name": "apply_patch",
                    "input": patch,
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        ("uv run pytest tests/tasks", "success", 0)
    ]
    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("src/changed.py", "apply_patch")
    ]


async def test_codex_tracks_apply_patch_inside_functions_exec(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    patch = "*** Begin Patch\n*** Update File: src/changed.py\n@@\n-old\n+new\n*** End Patch\n"
    source = (
        f"const patch = {json.dumps(patch)};\n"
        "const result = await tools.apply_patch(patch); text(result);"
    )
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "wrapped-patch",
                    "name": "exec",
                    "input": source,
                },
                BASE_TIME,
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [("src/changed.py", "exec")]


async def test_codex_ingests_unified_exec_failure_event(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    command = "uv run pytest tests/tasks/test_example.py::test_behavior -q"
    _write_jsonl(
        transcript,
        [
            {
                "type": "event_msg",
                "timestamp": BASE_TIME.isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-failed",
                        "command": ["/bin/zsh", "-lc", command],
                        "status": "failed",
                        "exit_code": 1,
                        "aggregated_output": "FAILED test_behavior - AssertionError",
                    },
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, "failure", 1)
    ]


@pytest.mark.asyncio
async def test_codex_authoritative_exec_supersedes_successful_outer_wrapper(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "codex-wrapper.jsonl"
    command = "uv run pytest tests/tasks/test_example.py::test_behavior -q"
    rewritten = "uv run rtk pytest tests/tasks/test_example.py::test_behavior -q"
    failed_output = "FAILED test_behavior - AssertionError"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME,
            ),
            {
                "type": "event_msg",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "native-exec",
                        "command": ["/bin/zsh", "-lc", rewritten],
                        "status": "failed",
                        "exit_code": 1,
                        "aggregated_output": failed_output,
                    },
                },
            },
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                        },
                        {"type": "input_text", "text": failed_output},
                    ],
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (rewritten, "failure", 1)
    ]


@pytest.mark.asyncio
async def test_codex_compound_timeout_preserves_completed_segment_outcomes(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "codex-timeout.jsonl"
    command = "uv run ruff check src && uv run pytest tests/unit -q"
    completed = "uv run ruff check src"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME,
            ),
            {
                "type": "event_msg",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "completed-segment",
                        "command": ["/bin/zsh", "-lc", completed],
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": "All checks passed!",
                    },
                },
            },
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": "command timed out before pytest completed",
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [
        (run.command, run.categories, run.outcome, run.exit_code)
        for run in evidence.validation_runs
    ] == [
        (completed, ("lint", "type_check"), "success", 0),
        (command, ("lint", "type_check", "test"), "unknown", None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_outcome"),
    [(0, "success"), (1, "failure")],
)
async def test_codex_ingests_json_style_functions_exec_evidence(
    tmp_path: Path,
    exit_code: int,
    expected_outcome: str,
) -> None:
    transcript = tmp_path / "codex-functions-exec.jsonl"
    command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-json-exec",
                    "name": "exec",
                    "input": (
                        f'const r = await tools.exec_command({{"cmd":{json.dumps(command)}}}); '
                        "text(JSON.stringify(r));"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-json-exec",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                        },
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {
                                    "chunk_id": "focused",
                                    "wall_time_seconds": 0.5,
                                    "exit_code": exit_code,
                                    "output": "focused result",
                                }
                            ),
                        },
                    ],
                },
                BASE_TIME + timedelta(seconds=1),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, expected_outcome, exit_code)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_outcome"),
    [(0, "success"), (7, "failure")],
)
async def test_codex_direct_exec_command_accepts_native_terminal_envelope(
    tmp_path: Path,
    exit_code: int,
    expected_outcome: str,
) -> None:
    transcript = tmp_path / "codex-direct-native.jsonl"
    command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q"
    envelope = (
        "Chunk ID: 1d32cc\n"
        "Wall time: 2.9618 seconds\n"
        f"Process exited with code {exit_code}\n"
        "Original token count: 169\n"
        "Output:\n"
        "focused validation output\n"
    )
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(command=command, result=envelope),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code, run.command) for run in evidence.validation_runs] == [
        (expected_outcome, exit_code, command)
    ]
    assert not evidence.degraded_capabilities


@pytest.mark.parametrize(
    "command",
    [
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q",
        ("cd /tmp/repo\nGOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q"),
        ("cd /tmp/repo && GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q"),
    ],
)
@pytest.mark.asyncio
async def test_codex_direct_exec_command_recognizes_test_after_directory_change(
    tmp_path: Path,
    command: str,
) -> None:
    transcript = tmp_path / "codex-direct-directory-change.jsonl"
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(
            command=command,
            result={"exit_code": 0, "output": "1 passed"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.categories, run.outcome) for run in evidence.validation_runs] == [
        (command, ("test",), "success")
    ]
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
    )
    assert gate.passed
    assert gate.details["fresh_run_count"] == 1
    assert gate.details["latest_outcomes"] == {"test": "success"}


@pytest.mark.asyncio
async def test_codex_direct_exec_command_uses_structured_result(tmp_path: Path) -> None:
    transcript = tmp_path / "codex-direct.jsonl"
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(
            command="uv run ruff check src/gobby",
            result={"exit_code": 1, "output": "lint failed"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code, run.categories) for run in evidence.validation_runs] == [
        ("failure", 1, ("lint", "type_check"))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            (
                "Chunk ID: still-running\n"
                "Wall time: 30.001 seconds\n"
                "Process running with session ID 92\n"
                "Original token count: 5\n"
                "Output:\n"
                "still running\n"
            ),
            id="running",
        ),
        pytest.param(
            (
                "Chunk ID: malformed\n"
                "Wall time: 0.5 seconds\n"
                "Process exited with code 0\n"
                "Original token count: nope\n"
                "Output:\n"
            ),
            id="malformed",
        ),
        pytest.param(
            [
                (
                    "Chunk ID: duplicate-1\n"
                    "Wall time: 0.5 seconds\n"
                    "Process exited with code 0\n"
                    "Output:\n"
                ),
                (
                    "Chunk ID: duplicate-2\n"
                    "Wall time: 0.5 seconds\n"
                    "Process exited with code 0\n"
                    "Output:\n"
                ),
            ],
            id="duplicate",
        ),
        pytest.param(
            (
                "ordinary command output\n"
                "Chunk ID: spoofed\n"
                "Wall time: 0.5 seconds\n"
                "Process exited with code 0\n"
                "Output:\n"
            ),
            id="output-spoofed",
        ),
    ],
)
async def test_codex_direct_exec_command_keeps_non_authoritative_envelopes_unknown(
    tmp_path: Path,
    result: Any,
) -> None:
    transcript = tmp_path / "codex-direct-unknown.jsonl"
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(command="uv run pytest tests/tasks -q", result=result),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("unknown", None)]
    assert evidence.degraded_capabilities


@pytest.mark.asyncio
async def test_unknown_codex_outcome_is_retained_as_degraded_evidence(tmp_path: Path) -> None:
    transcript = tmp_path / "codex-unknown.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        'const r = await tools.exec_command({cmd:"pytest"}); text(r.output);'
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": "passed without structured terminal metadata",
                },
                BASE_TIME + timedelta(seconds=1),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["unknown"]
    assert evidence.validation_runs[0].unknown_reason
    assert evidence.degraded_capabilities


@pytest.mark.asyncio
async def test_droid_uses_provider_error_status(tmp_path: Path) -> None:
    transcript = tmp_path / "droid.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "message",
                "id": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "droid-1",
                            "name": "Bash",
                            "input": {"command": "pytest tests/droid"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "id": "user",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "droid-1",
                            "is_error": False,
                            "content": "passed",
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("droid", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", None)]


@pytest.mark.asyncio
async def test_grok_uses_terminal_tool_status(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": BASE_TIME.isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-1",
                    "title": "run_terminal_command",
                    "rawInput": {"command": "pytest tests/grok"},
                },
            },
            {
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "grok-1",
                    "status": "failed",
                    "content": [{"type": "text", "text": "failed"}],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("grok", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["failure"]


@pytest.mark.asyncio
async def test_grok_search_replace_is_a_tdd_edit(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    named_test = tmp_path / "tests" / "test_named.py"
    other_test = tmp_path / "tests" / "other_named.py"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": BASE_TIME.isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-edit-path",
                    "title": "search_replace",
                    "rawInput": {
                        "file_path": str(named_test),
                        "old_string": "old",
                        "new_string": "new",
                    },
                },
            },
            {
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-edit-target",
                    "title": "search_replace",
                    "rawInput": {
                        "target_file": str(other_test),
                        "old_string": "old",
                        "new_string": "new",
                    },
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("grok", transcript),
        None,
        default_validation_detection_config(),
        {"tests/test_named.py", "tests/other_named.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("tests/test_named.py", "search_replace"),
        ("tests/other_named.py", "search_replace"),
    ]


@pytest.mark.asyncio
async def test_grok_completed_status_with_runner_failures_is_failure(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": BASE_TIME.isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-pytest",
                    "title": "run_terminal_command",
                    "rawInput": {
                        "command": (
                            "uv run pytest tests/storage/"
                            "test_postgres_agent_authorization.py::"
                            "test_project_checkouts_are_machine_isolated_lock_only_and_daemon_writable"
                        )
                    },
                },
            },
            {
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "grok-pytest",
                    "status": "completed",
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": (
                                    "Pytest: 1 passed, 1 failed\n\n"
                                    "Failures:\n"
                                    "     tests/storage/test_postgres_agent_authorization.py:614: "
                                    "in test_project_checkouts_are_machine_isolated_lock_only_and_daemon_writable\n"
                                    "     assert locked == rows\n"
                                    "     E   AssertionError: assert [] == [(UUID('4b0d7'))]\n"
                                ),
                            },
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("grok", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["failure"]


@pytest.mark.asyncio
async def test_qwen_uses_tool_call_result_status(tmp_path: Path) -> None:
    transcript = tmp_path / "qwen.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "qwen-1",
                                "name": "run_shell_command",
                                "args": {"command": "pytest tests/qwen"},
                            }
                        }
                    ],
                },
            },
            {
                "type": "tool_result",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "toolCallResult": {"callId": "qwen-1", "status": "success"},
                "message": {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": "qwen-1",
                                "name": "run_shell_command",
                                "response": {"output": "passed"},
                            }
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("qwen", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["success"]


@pytest.mark.asyncio
async def test_configured_gzip_archive_is_used(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    session = _session("claude", None)
    archive = archive_dir / f"{session.external_id}.jsonl.gz"
    records = _claude_tool_pair(
        command="pytest tests/archive",
        call_id="archive-1",
        start=BASE_TIME,
        result="passed",
    )
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(record) for record in records) + "\n")

    evidence = await derive_transcript_evidence(
        session,
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
        archive_dir=str(archive_dir),
    )

    assert [run.command for run in evidence.validation_runs] == ["pytest tests/archive"]
    assert evidence.attempted_paths[-1] == str(archive)


@pytest.mark.asyncio
async def test_missing_transcript_reports_attempted_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.transcript_evidence.find_transcript_on_disk",
        lambda *_args, **_kwargs: None,
    )
    missing = tmp_path / "missing.jsonl"
    session = _session("claude", missing)

    with pytest.raises(TranscriptEvidenceUnavailable) as exc_info:
        await derive_transcript_evidence(
            session,
            None,
            default_validation_detection_config(),
            set(),
            str(tmp_path),
            archive_dir=str(tmp_path / "archive"),
        )

    error = exc_info.value
    assert error.retry_after == 5
    assert str(missing) in error.attempted_paths
    assert str(tmp_path / "archive" / f"{session.external_id}.jsonl.gz") in error.attempted_paths


def test_agy_session_resolves_transcript_through_provider_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    external_id = "transcript-evidence-agy-1"
    target = (
        tmp_path
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / external_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    session = _session("agy", None)
    path, attempted = _resolve_transcript_path(session, None)
    assert path == str(target)
    assert str(target) in attempted


@pytest.mark.asyncio
async def test_unreadable_gzip_is_evidence_unavailable(tmp_path: Path) -> None:
    archive = tmp_path / "broken.jsonl.gz"
    archive.write_text("not gzip")

    with pytest.raises(TranscriptEvidenceUnavailable, match="could not be read"):
        await derive_transcript_evidence(
            _session("claude", archive),
            None,
            default_validation_detection_config(),
            set(),
            str(tmp_path),
        )


def test_merge_orders_cross_session_evidence() -> None:
    later_run = TranscriptValidationRun(
        session_id="session-2",
        source="codex",
        command="pytest later",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="success",
        started_at=BASE_TIME + timedelta(seconds=3),
        completed_at=BASE_TIME + timedelta(seconds=4),
        order=1,
    )
    earlier_run = TranscriptValidationRun(
        session_id="session-1",
        source="claude",
        command="pytest earlier",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure",
        started_at=BASE_TIME,
        completed_at=BASE_TIME + timedelta(seconds=1),
        order=2,
    )
    edit = TranscriptEdit(
        session_id="session-1",
        source="claude",
        path="src/changed.py",
        timestamp=BASE_TIME + timedelta(seconds=2),
        order=3,
        tool_name="Edit",
    )

    merged = merge_transcript_evidence(
        TranscriptEvidence(
            validation_runs=(later_run,),
            sessions=("session-2",),
            attempted_paths=("/two",),
        ),
        TranscriptEvidence(
            validation_runs=(earlier_run,),
            edits=(edit,),
            sessions=("session-1",),
            attempted_paths=("/one",),
        ),
    )

    assert [run.command for run in merged.validation_runs] == [
        "pytest earlier",
        "pytest later",
    ]
    assert merged.sessions == ("session-2", "session-1")
    assert merged.attempted_paths == ("/two", "/one")


def _session_run(
    session_id: str,
    command: str,
    completed_at: datetime,
    order: int,
    outcome: EvidenceOutcome = "success",
) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id=session_id,
        source="claude",
        command=command,
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome=outcome,
        started_at=completed_at - timedelta(seconds=1),
        completed_at=completed_at,
        order=order,
    )


def _session_edit(session_id: str, path: str, timestamp: datetime, order: int) -> TranscriptEdit:
    return TranscriptEdit(
        session_id=session_id,
        source="claude",
        path=path,
        timestamp=timestamp,
        order=order,
        tool_name="Edit",
    )


def test_merge_renumbers_order_across_sessions() -> None:
    """A fresh closing session's low local order must not sort before the owner's history."""
    owner = TranscriptEvidence(
        edits=(_session_edit("owner", "tests/test_feature.py", BASE_TIME, 300),),
        validation_runs=(
            _session_run("owner", "pytest red", BASE_TIME + timedelta(seconds=1), 305, "failure"),
        ),
    )
    closer = TranscriptEvidence(
        edits=(_session_edit("closer", "src/feature.py", BASE_TIME + timedelta(seconds=2), 10),),
        validation_runs=(
            _session_run("closer", "pytest green", BASE_TIME + timedelta(seconds=3), 15),
        ),
    )

    merged = merge_transcript_evidence(owner, closer)

    merged_items: list[TranscriptEdit | TranscriptValidationRun] = [
        *merged.edits,
        *merged.validation_runs,
    ]
    positions = sorted(merged_items, key=lambda item: item.order)
    assert [(item.session_id, item.order) for item in positions] == [
        ("owner", 1),
        ("owner", 2),
        ("closer", 3),
        ("closer", 4),
    ]
    assert [run.command for run in merged.validation_runs] == ["pytest red", "pytest green"]


def test_merge_keeps_intra_session_order_under_clock_skew() -> None:
    """Sessions interleave by timestamp, but a session's own transcript order never changes."""
    skewed = TranscriptEvidence(
        edits=(_session_edit("skewed", "src/a.py", BASE_TIME + timedelta(seconds=5), 1),),
        validation_runs=(
            _session_run("skewed", "pytest skewed", BASE_TIME + timedelta(seconds=4), 2),
        ),
    )
    other = TranscriptEvidence(
        edits=(
            _session_edit(
                "other", "src/b.py", BASE_TIME + timedelta(seconds=4, milliseconds=500), 1
            ),
        ),
    )

    merged = merge_transcript_evidence(skewed, other)

    merged_items: list[TranscriptEdit | TranscriptValidationRun] = [
        *merged.edits,
        *merged.validation_runs,
    ]
    positions = sorted(merged_items, key=lambda item: item.order)
    assert [(item.session_id, item.order) for item in positions] == [
        ("other", 1),
        ("skewed", 2),
        ("skewed", 3),
    ]


# ---------------------------------------------------------------------------
# A shell reports the status of the LAST element of a list or pipeline, so
# `pytest ... | tail`, `pytest ... ; echo`, and `pytest ... && other` record a
# zero status for a genuinely failing run. For those compound segments the
# runner's own terminal summary decides the outcome instead.
# ---------------------------------------------------------------------------

# Verbatim tail of a real red pytest run whose exit code a trailing `echo` zeroed.
_RED_PYTEST_OUTPUT = (
    "tests/memory/test_search_ranking.py ................FFFFF                [100%]\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/memory/test_search_ranking.py::test_embed_text_absent_preserves_yake_path\n"
    "========================= 5 failed, 16 passed in 0.30s =========================\n"
    "EXIT=0\n"
)

# Real shape of a PASSING `gobby test-types audit` ratchet over a non-empty
# baseline: the headline "Errors:" counts every baselined finding, and
# "Files scanned: 2\nErrors: 10" is one newline-crossing match away from
# reading as "2 errors" (#20880's misfiled type_check run).
_PASSING_TEST_TYPES_AUDIT_OUTPUT = (
    "Test types audit\n"
    "Files scanned: 2\n"
    "Errors: 10\n"
    "Codes: mypy:arg-type=7, mypy:assignment=3\n"
    "Baseline: loaded (.gobby/test-types-baseline.json)\n"
    "Baseline mode: diff\n"
    "New errors: 0\n"
    "Known baseline errors: 10\n"
    "Failing new errors >= high: 0\n"
    "\n"
    "Ranked files:\n"
    "    7 tests/mcp_proxy/tools/test_task_expansion_new.py [mypy:arg-type]\n"
    "    3 tests/tasks/test_validation.py [mypy:assignment]\n"
)

# The same audit with new errors above the threshold: exit 1, and the ratchet's
# own failing tally line is the runner-summary proof for wrapper-zeroed runs.
_FAILING_TEST_TYPES_AUDIT_OUTPUT = (
    "Test types audit\n"
    "Files scanned: 2\n"
    "Errors: 12\n"
    "Codes: mypy:arg-type=9, mypy:assignment=3\n"
    "Baseline: loaded (.gobby/test-types-baseline.json)\n"
    "Baseline mode: diff\n"
    "New errors: 2\n"
    "Known baseline errors: 10\n"
    "Failing new errors >= high: 2\n"
    "\n"
    "New failing errors:\n"
    '  tests/tasks/test_validation.py:41: error: Argument 1 to "close_task" has'
    ' incompatible type "None"; expected "str" [mypy:arg-type]\n'
)

# `gobby test-quality audit` with a non-empty baseline shares the shape but says
# "Issues"; it must stay clean by contract, not by accident of wording (#20880).
_PASSING_TEST_QUALITY_AUDIT_OUTPUT = (
    "Test quality audit\n"
    "Files scanned: 3\n"
    "Tests scanned: 41\n"
    "Issues: 6\n"
    "Severity: high=2, medium=4\n"
    "Codes: no-assertions=2, mystery-guest=4\n"
    "Baseline: loaded (.gobby/test-quality-baseline.json)\n"
    "Baseline mode: diff\n"
    "New issues: 0\n"
    "Known baseline issues: 6\n"
    "Failing new issues >= high: 0\n"
    "\n"
    "Known baseline issues:\n"
    "  HIGH no-assertions tests/tasks/test_validation.py::test_gate:12 - no assertions\n"
)

_FAILING_TEST_QUALITY_AUDIT_OUTPUT = (
    "Test quality audit\n"
    "Files scanned: 3\n"
    "Tests scanned: 41\n"
    "Issues: 7\n"
    "Severity: high=3, medium=4\n"
    "Codes: no-assertions=3, mystery-guest=4\n"
    "Baseline: loaded (.gobby/test-quality-baseline.json)\n"
    "Baseline mode: diff\n"
    "New issues: 1\n"
    "Known baseline issues: 6\n"
    "Failing new issues >= high: 1\n"
    "\n"
    "Failing new issues:\n"
    "  HIGH no-assertions tests/tasks/test_close.py::test_new:9 - no assertions\n"
)

_RUNNER_FAILURE_OUTPUTS = [
    pytest.param(_RED_PYTEST_OUTPUT, id="pytest-counted-summary"),
    pytest.param("===== 178 errors in 4.90s =====", id="pytest-collection-errors"),
    pytest.param("ERROR tests/x.py::test_y\nFAILED tests/x.py::test_z", id="pytest-summary-lines"),
    pytest.param("test result: FAILED. 1 passed; 1 failed; 0 ignored", id="cargo"),
    pytest.param("--- FAIL: TestThing (0.00s)\nFAIL\tgithub.com/a/b\t0.1s", id="go"),
    pytest.param("Found 2 errors in 1 file (checked 3 source files)", id="mypy"),
    pytest.param("Tests  3 failed | 5 passed (8)", id="vitest"),
    pytest.param(_FAILING_TEST_TYPES_AUDIT_OUTPUT, id="test-types-ratchet-failing"),
    pytest.param(_FAILING_TEST_QUALITY_AUDIT_OUTPUT, id="test-quality-ratchet-failing"),
]

_CLEAN_OUTPUTS = [
    pytest.param("21 passed in 0.13s", id="passing"),
    pytest.param("1208 passed, 64 deselected in 32.47s", id="passing-with-deselect"),
    pytest.param("0 failed, 5 passed in 0.10s", id="zero-failed"),
    pytest.param("3 passed, 1 xfailed, 2 warnings in 0.20s", id="xfailed"),
    pytest.param("Success: no issues found in 1830 source files", id="mypy-clean"),
    pytest.param("New errors: 0\nFailing new errors >= high: 0", id="ratchet-clean"),
    pytest.param("All checks passed!", id="ruff-clean"),
    pytest.param(
        _PASSING_TEST_TYPES_AUDIT_OUTPUT,
        id="test-types-ratchet-passing-with-baselined-errors",
    ),
    pytest.param(
        _PASSING_TEST_QUALITY_AUDIT_OUTPUT,
        id="test-quality-ratchet-passing-with-baselined-issues",
    ),
]


@pytest.mark.parametrize("output", _RUNNER_FAILURE_OUTPUTS)
def test_compound_zero_exit_yields_failure_when_the_runner_reported_failures(
    output: str,
) -> None:
    """A.1: aggregate shell status cannot prove a compound segment passed."""
    outcome, exit_code, unknown_reason = transcript_outcomes.extract_outcome(
        {"exit_code": 0, "stdout": output},
        output,
        aggregate_status_is_trustworthy=False,
    )

    assert outcome == "failure"
    assert exit_code == 0
    assert unknown_reason is None


@pytest.mark.parametrize("output", _RUNNER_FAILURE_OUTPUTS)
def test_compound_provider_success_is_also_overridden(output: str) -> None:
    """A.1: the provider `success`/`is_error` fallbacks lie the same way as `$?`."""
    outcome, _exit_code, _reason = transcript_outcomes.extract_outcome(
        {"success": True, "stdout": output},
        output,
        aggregate_status_is_trustworthy=False,
    )

    assert outcome == "failure"


@pytest.mark.parametrize("output", _CLEAN_OUTPUTS)
def test_clean_run_output_is_not_misread_as_failure(output: str) -> None:
    """A.2: nothing else changes.

    Clean output stays a success even for a compound segment, and a non-compound
    run's recorded exit code stays authoritative whatever the output says.
    """
    compound, _code, _reason = transcript_outcomes.extract_outcome(
        {"exit_code": 0, "stdout": output},
        output,
        aggregate_status_is_trustworthy=False,
    )
    assert compound == "success"

    plain, _code, _reason = transcript_outcomes.extract_outcome(
        {"exit_code": 0, "stdout": _RED_PYTEST_OUTPUT},
        _RED_PYTEST_OUTPUT,
        aggregate_status_is_trustworthy=True,
    )
    assert plain == "success"


def test_nonzero_exit_stays_a_failure_however_clean_the_output() -> None:
    """A.4: the new rule only ever adds failures, never removes one."""
    for trustworthy in (True, False):
        outcome, exit_code, _reason = transcript_outcomes.extract_outcome(
            {"exit_code": 1, "stdout": "21 passed in 0.13s"},
            "21 passed in 0.13s",
            aggregate_status_is_trustworthy=trustworthy,
        )
        assert (outcome, exit_code) == ("failure", 1)


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param(' 2>&1; echo "EXIT=$?"', id="trailing-echo"),
        pytest.param(" 2>&1 | tail -20", id="pipe-to-tail"),
        pytest.param(" && echo done", id="and-then"),
    ],
)
@pytest.mark.asyncio
async def test_wrapper_zeroed_exit_code_still_yields_a_failure_run(
    tmp_path: Path,
    suffix: str,
) -> None:
    """A.3: the end-to-end derivation stops filing wrapper-zeroed reds as passes."""
    transcript = tmp_path / "zeroed.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=f"GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_search_ranking.py -q{suffix}",
            call_id="red-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": _RED_PYTEST_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("failure", 0)]
    assert evidence.degraded_capabilities == ()


@pytest.mark.asyncio
async def test_uncompounded_passing_run_is_still_a_success(tmp_path: Path) -> None:
    """The overwhelmingly common shape keeps its byte-for-byte previous behavior."""
    transcript = tmp_path / "plain.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command="GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_search_ranking.py -q",
            call_id="green-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "21 passed in 0.13s"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", 0)]


_TEST_TYPES_AUDIT_COMMAND = (
    "uv run gobby test-types audit tests/tasks/test_validation.py "
    "--baseline .gobby/test-types-baseline.json --fail-on-new"
)


def test_failing_audit_nonzero_exit_stays_a_failure_in_both_trust_modes() -> None:
    """#20880: a genuinely failing ratchet keeps its exit-1 failure, bare or compound."""
    for trustworthy in (True, False):
        outcome, exit_code, _reason = transcript_outcomes.extract_outcome(
            {"exit_code": 1, "stdout": _FAILING_TEST_TYPES_AUDIT_OUTPUT},
            _FAILING_TEST_TYPES_AUDIT_OUTPUT,
            aggregate_status_is_trustworthy=trustworthy,
        )
        assert (outcome, exit_code) == ("failure", 1)


@pytest.mark.asyncio
async def test_passing_test_types_audit_is_recorded_as_a_successful_type_check(
    tmp_path: Path,
) -> None:
    """#20880: exit 0 with a passing ratchet is a success even for a compound run.

    The audit's headline counts baselined errors ("Errors: 10"), and the compound
    `cd ... && audit` shape routed that output through the runner-summary rule,
    filing the passing run as a failing type_check.
    """
    transcript = tmp_path / "audit.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=_TEST_TYPES_AUDIT_COMMAND,
            call_id="audit-bare",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": _PASSING_TEST_TYPES_AUDIT_OUTPUT},
        )
        + _claude_tool_pair(
            command=f"cd /repo && {_TEST_TYPES_AUDIT_COMMAND}",
            call_id="audit-compound",
            start=BASE_TIME + timedelta(minutes=1),
            result={"exit_code": 0, "stdout": _PASSING_TEST_TYPES_AUDIT_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.categories, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (("type_check",), "success", 0),
        (("type_check",), "success", 0),
    ]


def _codex_nested_exec_pair(*, command: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """A `tools.exec_command` call nested inside `exec`, which Codex outcomes drive."""
    return [
        _codex_response_item(
            {
                "type": "custom_tool_call",
                "call_id": "outer-exec",
                "name": "exec",
                "input": (
                    f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); text(r);"
                ),
            },
            BASE_TIME,
        ),
        _codex_response_item(
            {
                "type": "custom_tool_call_output",
                "call_id": "outer-exec",
                "output": json.dumps(result),
            },
            BASE_TIME + timedelta(seconds=1),
        ),
    ]


@pytest.mark.asyncio
async def test_codex_wrapper_zeroed_exit_code_still_yields_a_failure_run(tmp_path: Path) -> None:
    """A.3: Codex records its own outcomes, and the same shell truth applies there.

    `_consume_codex_outcome` classifies the run itself rather than going through
    `_record_validation_run`, so a fix confined to the Claude path would leave every
    Codex session filing wrapper-zeroed reds as passes.
    """
    transcript = tmp_path / "codex-zeroed.jsonl"
    _write_jsonl(
        transcript,
        _codex_nested_exec_pair(
            command="uv run pytest tests/tasks -q 2>&1 | tail -20",
            result={"exit_code": 0, "output": _RED_PYTEST_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("failure", 0)]


@pytest.mark.asyncio
async def test_codex_uncompounded_passing_run_is_still_a_success(tmp_path: Path) -> None:
    """The Codex path keeps its previous behavior for the non-compound shape."""
    transcript = tmp_path / "codex-plain.jsonl"
    _write_jsonl(
        transcript,
        _codex_nested_exec_pair(
            command="uv run pytest tests/tasks -q",
            result={"exit_code": 0, "output": "21 passed in 0.13s"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", 0)]


def _timestamped(stamp: str) -> str:
    return json.dumps({"type": "assistant", "timestamp": stamp, "message": {}})


def test_window_selection_drops_only_lines_older_than_the_lookback() -> None:
    window_start = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    lines = [
        _timestamped("2026-07-20T12:00:00Z"),
        _timestamped((window_start - WINDOW_LOOKBACK - timedelta(minutes=1)).isoformat()),
        _timestamped((window_start - timedelta(minutes=5)).isoformat()),
        _timestamped(window_start.isoformat()),
        _timestamped("2026-07-27T14:00:00Z"),
        json.dumps({"type": "summary", "summary": "no timestamp here"}),
        _timestamped("2026-07-20T12:00:00+02:00"),
    ]

    selected = list(select_window_raw_lines(lines, window_start))

    assert [item.raw_line_no for item in selected] == [2, 3, 4, 5, 6]
    assert [item.text for item in selected] == [lines[index] for index in (2, 3, 4, 5, 6)]


def test_window_selection_keeps_a_line_whose_nested_timestamp_is_older() -> None:
    window_start = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    # A Claude JSONL line carries its own timestamp last; a structured result
    # embedded earlier can carry an older one of its own.
    line = json.dumps(
        {
            "type": "user",
            "toolUseResult": {"timestamp": "2026-07-01T09:00:00Z"},
            "timestamp": window_start.isoformat(),
        }
    )

    selected = list(select_window_raw_lines([line], window_start))

    assert [item.text for item in selected] == [line]


def test_window_selection_keeps_everything_without_a_window() -> None:
    lines = [_timestamped("2026-07-20T12:00:00Z"), _timestamped("2026-07-27T14:00:00Z")]

    selected = list(select_window_raw_lines(lines, None))

    assert [item.text for item in selected] == lines
    assert [item.raw_line_no for item in selected] == [0, 1]


@pytest.mark.asyncio
async def test_pre_window_history_does_not_change_derived_evidence(tmp_path: Path) -> None:
    window_start = BASE_TIME
    transcript = tmp_path / "claude.jsonl"
    in_window = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_example.py",
        call_id="fresh",
        start=window_start + timedelta(minutes=1),
        result={"exit_code": 0, "stdout": "passed"},
    )
    ancient = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_ancient.py",
        call_id="ancient",
        start=window_start - timedelta(days=3),
        result={"exit_code": 1, "stdout": "1 failed"},
    )
    _write_jsonl(transcript, [*ancient, *in_window])
    session = _session("claude", transcript)

    evidence = await derive_transcript_evidence(
        session,
        window_start,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.command for run in evidence.validation_runs] == [
        "uv run pytest tests/tasks/test_example.py"
    ]
    assert [run.outcome for run in evidence.validation_runs] == ["success"]


@pytest.mark.asyncio
async def test_compound_run_records_only_its_validation_segments(tmp_path: Path) -> None:
    """Cover scoping needs the validation argv, not the git/shell segments around it."""
    transcript = tmp_path / "compound.jsonl"
    command = (
        'git stash push -m "tmp" src/gobby/servers/auth.py -q\n'
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_auth.py -q\n"
        "git stash pop -q"
    )
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=command,
            call_id="red-1",
            start=BASE_TIME,
            result={"exit_code": 1, "stdout": _RED_PYTEST_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.command for run in evidence.validation_runs] == [command]
    assert [run.validation_segments for run in evidence.validation_runs] == [
        (
            TranscriptValidationSegment(
                command="pytest tests/servers/test_auth.py -q", categories=("test",)
            ),
        )
    ]


async def test_compound_run_records_every_segment_with_its_categories(tmp_path: Path) -> None:
    """A format-then-test compound run belongs to both categories, one segment each."""
    transcript = tmp_path / "compound-green.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=(
                "uv run ruff format --check src/gobby/x.py && "
                "GOBBY_TEST_PROTECT=1 uv run pytest tests/unit -q"
            ),
            call_id="green-1",
            start=BASE_TIME,
            result={
                "exit_code": 0,
                "stdout": (
                    "1 file already formatted\n"
                    "tests/unit/test_a.py ..                                    [100%]\n"
                    "2 passed in 0.10s\n"
                ),
            },
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    (run,) = evidence.validation_runs
    assert run.categories == ("format", "test")
    assert run.validation_segments == (
        TranscriptValidationSegment(
            command="ruff format --check src/gobby/x.py", categories=("format",)
        ),
        TranscriptValidationSegment(command="pytest tests/unit -q", categories=("test",)),
    )


async def test_edits_in_another_checkout_match_task_files_by_suffix(tmp_path: Path) -> None:
    """Edits made in a worktree count when the close resolves the main checkout."""
    repo_path = tmp_path / "main"
    worktree = tmp_path / "worktrees" / "task-1"
    patch = (
        f"*** Begin Patch\n*** Update File: {worktree / 'src' / 'changed.py'}\n"
        "@@\n-old\n+new\n*** End Patch\n"
    )
    codex_transcript = tmp_path / "codex.jsonl"
    _write_jsonl(
        codex_transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "wrapped-patch",
                    "name": "exec",
                    "input": (
                        f"const patch = {json.dumps(patch)};\n"
                        "const result = await tools.apply_patch(patch); text(result);"
                    ),
                },
                BASE_TIME,
            ),
        ],
    )
    claude_transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        claude_transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "edit-1",
                            "name": "Edit",
                            "input": {"file_path": str(worktree / "src" / "changed.py")},
                        }
                    ],
                },
            },
        ],
    )

    codex_evidence = await derive_transcript_evidence(
        _session("codex", codex_transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(repo_path),
    )
    claude_evidence = await derive_transcript_evidence(
        _session("claude", claude_transcript, suffix="2"),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(repo_path),
    )

    assert [(edit.path, edit.tool_name) for edit in codex_evidence.edits] == [
        ("src/changed.py", "exec")
    ]
    assert [(edit.path, edit.tool_name) for edit in claude_evidence.edits] == [
        ("src/changed.py", "Edit")
    ]


async def test_edit_outside_every_checkout_without_task_suffix_is_ignored(tmp_path: Path) -> None:
    repo_path = tmp_path / "main"
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "edit-1",
                            "name": "Edit",
                            "input": {"file_path": str(tmp_path / "elsewhere" / "other.py")},
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(repo_path),
    )

    assert evidence.edits == ()
