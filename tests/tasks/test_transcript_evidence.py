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
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptValidationRun,
    _extract_output,
    derive_transcript_evidence,
    merge_transcript_evidence,
)

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
