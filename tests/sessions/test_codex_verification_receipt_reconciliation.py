from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import VerificationReceiptStore
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.verification_receipt_ingestion import (
    VerificationReceiptIngestionError,
)

pytestmark = pytest.mark.unit

_CONTENT_ITEMS_FIXTURE = (
    Path(__file__).parents[1] / "fixtures/provider_contracts/codex/functions-exec-items.json"
)


def _response_item(payload: dict[str, Any], *, second: int = 0) -> str:
    return json.dumps(
        {
            "timestamp": f"2026-07-23T12:00:{second:02d}Z",
            "type": "response_item",
            "payload": payload,
        }
    )


def _call(call_id: str, tool_input: str) -> str:
    return _response_item(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": call_id,
            "name": "exec",
            "input": tool_input,
        }
    )


def _output(call_id: str, *texts: str) -> str:
    return _response_item(
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": text} for text in texts],
        },
        second=1,
    )


def _direct_call(call_id: str, command: str) -> str:
    return _response_item(
        {
            "type": "function_call",
            "call_id": call_id,
            "name": "exec_command",
            "arguments": json.dumps({"cmd": command}),
        }
    )


def _direct_output(call_id: str, exit_code: int, *, second: int = 1) -> str:
    return _response_item(
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": (
                "Chunk ID: sanitized\n"
                "Wall time: 0.1234 seconds\n"
                f"Process exited with code {exit_code}\n"
                "Final output:\n"
                "focused verification output\n"
            ),
        },
        second=second,
    )


def _direct_running_output(call_id: str, session_id: int) -> str:
    return _response_item(
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": (
                "Chunk ID: sanitized\n"
                "Wall time: 30.1234 seconds\n"
                f"Process running with session ID {session_id}\n"
                "Original token count: 5\n"
                "Output:\n"
                "still running\n"
            ),
        },
        second=3,
    )


def _setup_processor(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    transcript_path: Path,
    *,
    external_suffix: str,
) -> tuple[SessionMessageProcessor, Any, Any]:
    session = session_manager.register(
        external_id=f"codex-receipt-{external_suffix}",
        machine_id="machine-codex-receipt",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        f"Codex receipt {external_suffix}",
        claimed_by_session_id=session.id,
        validation_criteria="The Codex transcript produces the expected verification receipt.",
    )
    SessionVariableManager(temp_db).set_variable(session.id, "active_task_id", task.id)
    processor = SessionMessageProcessor(
        temp_db,
        session_manager=session_manager,
    )
    processor.register_session(session.id, str(transcript_path), source="codex")
    return processor, session, task


@pytest.mark.parametrize(
    ("exit_code", "normalized_outcome"),
    [(0, "success"), (7, "failure")],
)
async def test_direct_exec_command_persists_exact_terminal_receipt(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
    exit_code: int,
    normalized_outcome: str,
) -> None:
    call_id = f"call_direct_{exit_code}"
    command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/ -q"
    transcript_path = tmp_path / f"rollout-direct-{exit_code}.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _direct_call(call_id, command),
                _direct_output(call_id, exit_code),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix=f"direct-{exit_code}",
    )

    await processor._process_session(session.id, str(transcript_path), at_eof=True)

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    assert receipts[0].execution_id == f"{call_id}:0"
    assert receipts[0].command == command
    assert receipts[0].exit_code == exit_code
    assert receipts[0].normalized_outcome == normalized_outcome


async def test_nested_exec_write_stdin_persists_exact_terminal_receipt(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    call_id = "call_nested_write_stdin"
    running_poll_id = "poll_nested_write_stdin_running"
    terminal_poll_id = "poll_nested_write_stdin_terminal"
    command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/ -q"
    transcript_path = tmp_path / "rollout-nested-write-stdin.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _call(
                    call_id,
                    f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); text(r);",
                ),
                _output(
                    call_id,
                    json.dumps({"session_id": 93, "output": "still running"}),
                ),
                _response_item(
                    {
                        "type": "function_call",
                        "call_id": running_poll_id,
                        "name": "write_stdin",
                        "arguments": json.dumps({"session_id": 93, "chars": ""}),
                    },
                    second=2,
                ),
                _direct_running_output(running_poll_id, 93),
                _response_item(
                    {
                        "type": "function_call",
                        "call_id": terminal_poll_id,
                        "name": "write_stdin",
                        "arguments": json.dumps({"session_id": 93, "chars": ""}),
                    },
                    second=4,
                ),
                _direct_output(terminal_poll_id, 0, second=5),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix="nested-write-stdin",
    )

    await processor._process_session(session.id, str(transcript_path), at_eof=True)

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    assert receipts[0].execution_id == f"{call_id}:0"
    assert receipts[0].command == command
    assert receipts[0].exit_code == 0
    assert receipts[0].normalized_outcome == "success"


@pytest.mark.parametrize(
    ("output", "expects_unknown_receipt"),
    [
        (
            (
                "Chunk ID: sanitized\n"
                "Wall time: 0.1234 seconds\n"
                "Process running with session ID 123\n"
                "Live output:\n"
            ),
            False,
        ),
        ('{"exit_code": 0, "output": "untrusted summary"}', True),
        ("human summary without a structured outcome", True),
    ],
)
async def test_direct_exec_command_catalogues_nonterminal_or_malformed_output_as_unknown(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
    output: str,
    expects_unknown_receipt: bool,
) -> None:
    call_id = "call_direct_unknown"
    transcript_path = tmp_path / "rollout-direct-unknown.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _direct_call(call_id, "uv run ruff check src/"),
                _response_item(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                    second=1,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix=f"direct-unknown-{len(output)}",
    )

    await processor._process_session(session.id, str(transcript_path), at_eof=True)

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    if not expects_unknown_receipt:
        assert receipts == []
        return
    assert len(receipts) == 1
    assert receipts[0].execution_id == f"{call_id}:0"
    assert receipts[0].command == "uv run ruff check src/"
    assert receipts[0].exit_code is None
    assert receipts[0].normalized_outcome == "unknown"
    assert processor._byte_offsets[session.id] == transcript_path.stat().st_size


async def test_fast_content_items_shape_persists_acknowledged_receipt(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    payload = json.loads(_CONTENT_ITEMS_FIXTURE.read_text())
    item = copy.deepcopy(payload["events"][0]["item"])
    call_id = "call_fast_content_items"
    transcript_path = tmp_path / "rollout.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _call(call_id, item["arguments"]),
                _response_item(
                    {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": {
                            "contentItems": item["contentItems"],
                            "status": item["status"],
                            "success": item["success"],
                        },
                    },
                    second=1,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix="fast-content-items",
    )

    await processor._process_session(session.id, str(transcript_path))

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    assert receipts[0].execution_id == "call_fast_content_items:0"
    assert receipts[0].normalized_outcome == "success"
    assert processor._byte_offsets[session.id] == transcript_path.stat().st_size


async def test_split_poll_projection_failure_restores_parser_and_progress(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call(
        "call_split",
        'const r = await tools.exec_command({cmd:"uv run ruff check src/gobby"}); text(r);',
    )
    output = _output(
        "call_split",
        json.dumps({"exit_code": 0, "output": "All checks passed"}),
    )
    transcript_path = tmp_path / "rollout.jsonl"
    transcript_path.write_text(call + "\n", encoding="utf-8")
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix="split-rollback",
    )
    await processor._process_session(session.id, str(transcript_path))
    committed_offset = processor._byte_offsets[session.id]
    parser_state = copy.deepcopy(processor._parsers[session.id].snapshot_state())
    with transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(output + "\n")

    original_projection = SessionVariableManager.upsert_bounded_list_variable
    attempts = 0

    def fail_once(manager: SessionVariableManager, *args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected projection failure")
        return original_projection(manager, *args, **kwargs)

    monkeypatch.setattr(
        SessionVariableManager,
        "upsert_bounded_list_variable",
        fail_once,
    )

    with pytest.raises(VerificationReceiptIngestionError):
        await processor._process_session(session.id, str(transcript_path))

    assert processor._byte_offsets[session.id] == committed_offset
    assert processor._parsers[session.id].snapshot_state() == parser_state

    reconciliation = await processor.reconcile_codex_transcript(session.id)

    assert reconciliation.flushed is True
    assert reconciliation.error is None
    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    assert receipts[0].execution_id == "call_split:0"
    assert processor._byte_offsets[session.id] == transcript_path.stat().st_size
    projections = SessionVariableManager(temp_db).get_variables(session.id)["verification_evidence"]
    assert len(projections) == 1
    assert projections[0]["receipt_count"] == 1


async def test_partial_multi_result_failure_replays_after_restart_without_duplicates(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript_path = tmp_path / "rollout.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _call(
                    "call_batch",
                    "const rs = await Promise.all(commands.map(cmd => tools.exec_command({cmd})));",
                ),
                _output(
                    "call_batch",
                    json.dumps(
                        {
                            "cmd": "GOBBY_TEST_PROTECT=1 uv run pytest tests/a.py -q",
                            "exit_code": 1,
                            "output": "failed",
                        }
                    ),
                    json.dumps(
                        {
                            "command": "uv run ruff check src/gobby",
                            "exitCode": 0,
                            "output": "passed",
                        }
                    ),
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix="partial-restart",
    )
    initial_parser_state = copy.deepcopy(processor._parsers[session.id].snapshot_state())
    original_projection = SessionVariableManager.upsert_bounded_list_variable
    attempts = 0

    def fail_second(manager: SessionVariableManager, *args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("injected second projection failure")
        return original_projection(manager, *args, **kwargs)

    monkeypatch.setattr(
        SessionVariableManager,
        "upsert_bounded_list_variable",
        fail_second,
    )

    with pytest.raises(VerificationReceiptIngestionError):
        await processor._process_session(session.id, str(transcript_path))

    assert session.id not in processor._byte_offsets
    assert processor._parsers[session.id].snapshot_state() == initial_parser_state

    restarted = SessionMessageProcessor(
        temp_db,
        session_manager=session_manager,
    )
    restarted.register_session(session.id, str(transcript_path), source="codex")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="gobby.sessions.processor_transcripts"):
        await restarted._process_session(session.id, str(transcript_path), at_eof=True)

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 2
    assert {receipt.execution_id for receipt in receipts} == {
        "call_batch:0",
        "call_batch:1",
    }
    projections = SessionVariableManager(temp_db).get_variables(session.id)["verification_evidence"]
    assert len(projections) == 1
    assert projections[0]["receipt_count"] == 2
    batch_records = [
        record
        for record in caplog.records
        if record.getMessage() == "Codex transcript verification receipt batch acknowledged"
    ]
    assert len(batch_records) == 1
    assert batch_records[0].levelno == logging.DEBUG
    assert vars(batch_records[0])["receipt_count"] == 2
    assert "Codex transcript verification receipt acknowledged" not in caplog.text
    assert "Derived Codex transcript verification outcomes" not in caplog.text


async def test_malformed_nested_output_commits_progress_with_unknown_receipt(
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "rollout.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _call(
                    "call_malformed",
                    'const r = await tools.exec_command({cmd:"uv run ruff check src/"}); text(r);',
                ),
                _output("call_malformed", "human summary without a structured outcome"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processor, session, task = _setup_processor(
        temp_db,
        session_manager,
        sample_project,
        transcript_path,
        external_suffix="malformed",
    )

    await processor._process_session(session.id, str(transcript_path), at_eof=True)

    receipts = VerificationReceiptStore(temp_db).list_for_task(
        sample_project["id"],
        task.id,
    )
    assert len(receipts) == 1
    assert receipts[0].execution_id == "call_malformed:0"
    assert receipts[0].command == "uv run ruff check src/"
    assert receipts[0].exit_code is None
    assert receipts[0].normalized_outcome == "unknown"
    assert processor._byte_offsets[session.id] == transcript_path.stat().st_size
