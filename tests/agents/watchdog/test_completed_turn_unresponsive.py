"""A delivered completed-turn reprompt that never starts a new turn is bounded."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.agents.watchdog.completed_turn_recovery import (
    CompletedTurnRecoveryHost,
    recover_completed_turn,
)
from gobby.agents.watchdog.models import (
    CompletedTurnRecoveryState,
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.workflows.step_context import StepWorkflowContext

_IDLE_TIMEOUT_SECONDS = 300


def _snapshot() -> WatchdogTranscriptSnapshot:
    event = TranscriptEventSummary(
        line_num=99,
        timestamp=datetime(2026, 9, 3, 2, 47, 35, tzinfo=UTC),
        event_type="response_item",
        payload_type="message",
    )
    return WatchdogTranscriptSnapshot(
        provider="codex",
        latest_turn_event=event,
        latest_turn_kind="completed",
    )


class _FakeHost:
    def __init__(self, *, work_finished: bool = False) -> None:
        self._completed_turn_recovery: dict[str, CompletedTurnRecoveryState] = {}
        self._tmux_config = TmuxConfig(max_reprompt_attempts=3)
        self._work_finished = work_finished
        self.failures: list[str] = []
        self.reprompts: list[str] = []
        self.completions = 0
        self.snapshot_logs: list[tuple[str, int]] = []

    async def _load_step_workflow_context(
        self, run: AgentRun
    ) -> tuple[StepWorkflowContext | None, bool]:
        return None, True

    async def _session_made_successful_mcp_call(self, run: AgentRun) -> bool | None:
        return True

    async def _idle_reprompt_message(
        self,
        run: AgentRun,
        *,
        step_context: StepWorkflowContext | None = None,
        context_resolved: bool = False,
    ) -> str:
        return "reprompt"

    async def _send_idle_reprompt(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        reprompt_message: str | None = None,
    ) -> bool:
        self.reprompts.append(reprompt_message or "")
        return True

    async def _record_watchdog_task_event(
        self,
        run: AgentRun,
        *,
        action: str,
        session_id: str | None,
        detail: str,
    ) -> None:
        return None

    async def _fail_idle_agent(self, run: AgentRun, reason: str) -> None:
        self.failures.append(reason)

    async def _complete_if_work_finished(self, run: AgentRun) -> bool:
        if self._work_finished:
            self.completions += 1
        return self._work_finished

    async def _log_transcript_snapshot(
        self,
        run: AgentRun,
        *,
        reason: str,
        snapshot: WatchdogTranscriptSnapshot | None = None,
        level: int = logging.WARNING,
    ) -> None:
        self.snapshot_logs.append((reason, level))


def _run() -> AgentRun:
    return cast(AgentRun, SimpleNamespace(id="run-1", child_session_id="sess-1"))


async def _recover(host: _FakeHost) -> int:
    return await recover_completed_turn(
        cast(CompletedTurnRecoveryHost, host),
        _run(),
        tmux_name="gobby-test",
        session_id="sess-1",
        transcript_path="/tmp/transcript.jsonl",
        snapshot=_snapshot(),
        idle_timeout_seconds=_IDLE_TIMEOUT_SECONDS,
    )


def _age_reprompt(host: _FakeHost, seconds: int) -> None:
    state = host._completed_turn_recovery["run-1"]
    assert state.last_reprompt_at is not None
    state.last_reprompt_at = datetime.now(UTC) - timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_duplicate_completed_turn_past_reprompt_deadline_fails_run() -> None:
    host = _FakeHost()

    assert await _recover(host) == 1
    assert host.reprompts == ["reprompt"]
    _age_reprompt(host, _IDLE_TIMEOUT_SECONDS + 1)

    assert await _recover(host) == 1
    assert host.reprompts == ["reprompt"]
    assert len(host.failures) == 1
    assert "completed-turn reprompt" in host.failures[0]
    assert f"{_IDLE_TIMEOUT_SECONDS}s" in host.failures[0]
    assert ("failing after unanswered completed-turn reprompt", logging.ERROR) in host.snapshot_logs


@pytest.mark.asyncio
async def test_duplicate_completed_turn_within_deadline_stays_quiet() -> None:
    host = _FakeHost()

    assert await _recover(host) == 1
    _age_reprompt(host, _IDLE_TIMEOUT_SECONDS - 1)

    assert await _recover(host) == 0
    assert host.reprompts == ["reprompt"]
    assert host.failures == []
    assert host.completions == 0


@pytest.mark.asyncio
async def test_completed_turn_with_finished_work_completes_before_reprompt() -> None:
    host = _FakeHost(work_finished=True)

    assert await _recover(host) == 1
    assert host.completions == 1
    assert host.reprompts == []
    assert host.failures == []
    assert host._completed_turn_recovery == {}
    assert ("completing idle agent whose work already finished", logging.INFO) in host.snapshot_logs
