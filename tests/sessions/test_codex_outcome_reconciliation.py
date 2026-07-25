from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent
from gobby.sessions.message_stats import MessageStats
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import VerificationReceiptStore
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures/provider_contracts/codex/terminal-functions-exec-rollout-0.144.6.jsonl"
)


class _ObserverHookManager:
    def __init__(self) -> None:
        self.events: list[HookEvent] = []

    async def handle_async(self, event: HookEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_processor_reconciles_exact_exit_codes_without_replaying_generic_mcp(
    tmp_path: Path,
    temp_db: Any,
    session_manager: Any,
    sample_project: dict[str, Any],
) -> None:
    fixture_lines = _FIXTURE.read_text().splitlines(keepends=True)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(fixture_lines[:8]))
    hook_manager = _ObserverHookManager()
    session = session_manager.register(
        external_id="codex-outcome-reconciliation",
        machine_id="machine-codex-outcome-reconciliation",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript),
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        "Codex outcome reconciliation",
        claimed_by_session_id=session.id,
    )
    SessionVariableManager(temp_db).set_variable(session.id, "active_task_id", task.id)
    processor = SessionMessageProcessor(
        temp_db,
        hook_manager=hook_manager,
        session_manager=session_manager,
    )
    processor._process_parsed_batch = AsyncMock(
        return_value=MessageStats(
            message_count=8,
            turn_count=0,
            tool_call_count=3,
            last_assistant_content=None,
        )
    )
    processor.register_session(session.id, str(transcript), source="codex")

    await processor._process_session(session.id, str(transcript))

    receipt_store = VerificationReceiptStore(temp_db)
    receipts = receipt_store.list_for_task(sample_project["id"], task.id)
    assert {receipt.execution_id: receipt.exit_code for receipt in receipts} == {
        "call_batch:0": 1,
        "call_batch:1": 0,
        "call_rerun:0": 0,
    }

    with transcript.open("a") as stream:
        stream.write("".join(fixture_lines[8:10]))
    yielded_result = await processor.reconcile_codex_transcript(session.id)

    assert yielded_result.flushed is True
    receipts = receipt_store.list_for_task(sample_project["id"], task.id)
    assert {receipt.execution_id: receipt.exit_code for receipt in receipts} == {
        "call_batch:0": 1,
        "call_batch:1": 0,
        "call_rerun:0": 0,
    }

    with transcript.open("a") as stream:
        stream.write("".join(fixture_lines[10:]))
    final_result = await processor.reconcile_codex_transcript(session.id)

    assert final_result.flushed is True
    receipts = receipt_store.list_for_task(sample_project["id"], task.id)
    assert {receipt.execution_id: receipt.exit_code for receipt in receipts} == {
        "call_batch:0": 1,
        "call_batch:1": 0,
        "call_rerun:0": 0,
        "call_yielded_batch:0": 7,
        "call_yielded_batch:1": 0,
    }
    assert {receipt.execution_id: receipt.command for receipt in receipts} == {
        "call_batch:0": "GOBBY_TEST_PROTECT=1 uv run pytest "
        "tests/workflows/test_provider_outcome_contracts.py -q",
        "call_batch:1": "git status --short",
        "call_rerun:0": "GOBBY_TEST_PROTECT=1 uv run pytest "
        "tests/workflows/test_provider_outcome_contracts.py -q",
        "call_yielded_batch:0": "sh -c 'exit 7'",
        "call_yielded_batch:1": "GOBBY_TEST_PROTECT=1 uv run pytest "
        "tests/workflows/test_provider_outcome_contracts.py -q",
    }

    replay_result = await processor.reconcile_codex_transcript(session.id)

    assert replay_result.flushed is True
    assert len(receipt_store.list_for_task(sample_project["id"], task.id)) == 5
    assert hook_manager.events == []


@pytest.mark.asyncio
async def test_reconcile_rejects_non_codex_registration(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    processor = SessionMessageProcessor(MagicMock())
    processor.register_session("claude-session", str(transcript), source="claude")

    result = await processor.reconcile_codex_transcript("claude-session")

    assert result.flushed is False
    assert result.error == "session is not registered as Codex"


async def test_reconcile_recovers_missing_codex_registration(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("")
    session_manager = MagicMock()
    session_manager.get.return_value = MagicMock(
        source="codex",
        transcript_path=str(transcript),
    )
    processor = SessionMessageProcessor(MagicMock(), session_manager=session_manager)
    processor._process_session = AsyncMock()

    result = await processor.reconcile_codex_transcript("platform-session")

    assert result.flushed is True
    assert processor._session_sources["platform-session"] == "codex"
    session_manager.get.assert_called_once_with("platform-session")
    processor._process_session.assert_awaited_once_with(
        "platform-session", str(transcript), at_eof=True
    )
