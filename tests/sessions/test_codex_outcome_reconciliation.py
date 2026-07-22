from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType
from gobby.sessions.message_stats import MessageStats
from gobby.sessions.processor import SessionMessageProcessor
from gobby.workflows.observer_verification import detect_verification_evidence

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures/provider_contracts/codex/terminal-functions-exec-rollout-0.144.6.jsonl"
)


class _ObserverHookManager:
    def __init__(self) -> None:
        self.events: list[HookEvent] = []
        self.variables: dict[str, object] = {}

    async def handle_async(self, event: HookEvent) -> None:
        self.events.append(event)
        detect_verification_evidence(event, self.variables, event.session_id)


@pytest.mark.asyncio
async def test_processor_reconciles_exact_exit_codes_without_replaying_generic_mcp(
    tmp_path: Path,
) -> None:
    fixture_lines = _FIXTURE.read_text().splitlines(keepends=True)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(fixture_lines[:8]))
    hook_manager = _ObserverHookManager()
    processor = SessionMessageProcessor(MagicMock(), hook_manager=hook_manager)
    processor._process_parsed_batch = AsyncMock(
        return_value=MessageStats(
            message_count=8,
            turn_count=0,
            tool_call_count=3,
            last_assistant_content=None,
        )
    )
    processor.register_session("platform-session", str(transcript), source="codex")

    await processor._process_session("platform-session", str(transcript))

    assert hook_manager.variables["verification_evidence_recorded"] is True
    assert [event.data["tool_outcome"]["exit_code"] for event in hook_manager.events] == [
        1,
        0,
        0,
    ]
    assert [event.data["call_id"] for event in hook_manager.events] == [
        "call_batch:0",
        "call_batch:1",
        "call_rerun:0",
    ]
    evidence = hook_manager.variables["verification_evidence"]
    assert isinstance(evidence, list)
    evidence_count_before_final_wait = len(evidence)

    with transcript.open("a") as stream:
        stream.write("".join(fixture_lines[8:10]))
    yielded_result = await processor.reconcile_codex_transcript("platform-session")

    assert yielded_result.flushed is True
    assert [event.data["tool_outcome"]["exit_code"] for event in hook_manager.events] == [
        1,
        0,
        0,
    ]

    with transcript.open("a") as stream:
        stream.write("".join(fixture_lines[10:]))
    final_result = await processor.reconcile_codex_transcript("platform-session")

    assert final_result.flushed is True
    assert [event.data["tool_outcome"]["exit_code"] for event in hook_manager.events] == [
        1,
        0,
        0,
        7,
        0,
    ]
    assert [event.data["call_id"] for event in hook_manager.events] == [
        "call_batch:0",
        "call_batch:1",
        "call_rerun:0",
        "call_yielded_batch:0",
        "call_yielded_batch:1",
    ]
    assert [event.data["tool_input"]["command"] for event in hook_manager.events[-2:]] == [
        "sh -c 'exit 7'",
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_provider_outcome_contracts.py -q",
    ]
    evidence = hook_manager.variables["verification_evidence"]
    assert isinstance(evidence, list)
    assert len(evidence) == evidence_count_before_final_wait + 2
    assert [item["success"] for item in evidence[-2:]] == [False, True]
    assert all(item["evidence_type"] == "shell_command" for item in evidence[-2:])

    replay_result = await processor.reconcile_codex_transcript("platform-session")

    assert replay_result.flushed is True
    assert len(hook_manager.events) == 5
    assert len(evidence) == evidence_count_before_final_wait + 2
    assert all(event.event_type == HookEventType.AFTER_TOOL for event in hook_manager.events)
    assert all(event.data["tool_name"] == "Bash" for event in hook_manager.events)


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
