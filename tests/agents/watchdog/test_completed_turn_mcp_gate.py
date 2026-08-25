"""Fail-fast for toolless runs pinned in MCP-only steps (no reprompt loop)."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.agents.watchdog.completed_turn_recovery import (
    CompletedTurnRecoveryHost,
    recover_completed_turn,
    step_requires_gobby_proxy,
)
from gobby.agents.watchdog.models import (
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.workflows.step_context import StepWorkflowContext

_MCP_ONLY_TOOLS = [
    "mcp__gobby__call_tool",
    "mcp__gobby__list_mcp_servers",
    "mcp__gobby__list_tools",
    "mcp__gobby__get_tool_schema",
]


def _step_context(
    allowed_tools: list[str] | str,
    *,
    is_entry_step: bool = True,
) -> StepWorkflowContext:
    return StepWorkflowContext(
        workflow_name="plan-adversary-taskless",
        current_step="load_skill",
        description=None,
        status_message=None,
        exit_condition=None,
        agent_name="plan-adversary-taskless",
        allowed_tools=cast("list[str]", allowed_tools),
        is_entry_step=is_entry_step,
    )


def _snapshot() -> WatchdogTranscriptSnapshot:
    event = TranscriptEventSummary(
        line_num=10,
        timestamp=datetime(2026, 8, 25, 4, 30, tzinfo=UTC),
        event_type="event_msg",
        payload_type="task_complete",
    )
    return WatchdogTranscriptSnapshot(
        provider="codex",
        latest_turn_event=event,
        latest_turn_kind="completed",
    )


class _FakeHost:
    def __init__(
        self,
        *,
        step_context: StepWorkflowContext | None,
        made_call: bool | None,
    ) -> None:
        self._completed_turn_recovery: dict[str, object] = {}
        self._tmux_config = TmuxConfig(max_reprompt_attempts=3)
        self._step_context = step_context
        self._made_call = made_call
        self.failures: list[str] = []
        self.reprompts: list[str] = []
        self.snapshot_logs: list[str] = []

    async def _load_step_workflow_context(
        self, run: AgentRun
    ) -> tuple[StepWorkflowContext | None, bool]:
        return self._step_context, True

    async def _session_made_successful_mcp_call(self, run: AgentRun) -> bool | None:
        return self._made_call

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

    async def _complete_if_step_workflow_finished(self, run: AgentRun) -> bool:
        return False

    async def _log_transcript_snapshot(
        self,
        run: AgentRun,
        *,
        reason: str,
        snapshot: WatchdogTranscriptSnapshot | None = None,
        level: int = logging.WARNING,
    ) -> None:
        self.snapshot_logs.append(reason)


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
    )


@pytest.mark.asyncio
async def test_toolless_run_in_mcp_only_step_fails_without_reprompts() -> None:
    host = _FakeHost(step_context=_step_context(_MCP_ONLY_TOOLS), made_call=False)

    assert await _recover(host) == 1
    assert len(host.failures) == 1
    assert "MCP-only entry step 'load_skill'" in host.failures[0]
    assert host.reprompts == []


@pytest.mark.asyncio
async def test_later_mcp_only_step_keeps_reprompt_path() -> None:
    host = _FakeHost(
        step_context=_step_context(_MCP_ONLY_TOOLS, is_entry_step=False),
        made_call=False,
    )

    assert await _recover(host) == 1
    assert host.failures == []
    assert len(host.reprompts) == 1


@pytest.mark.asyncio
async def test_run_with_successful_mcp_call_keeps_reprompt_path() -> None:
    host = _FakeHost(step_context=_step_context(_MCP_ONLY_TOOLS), made_call=True)

    assert await _recover(host) == 1
    assert host.failures == []
    assert len(host.reprompts) == 1


@pytest.mark.asyncio
async def test_step_allowing_non_mcp_tools_keeps_reprompt_path() -> None:
    host = _FakeHost(
        step_context=_step_context(["Bash", "mcp__gobby__call_tool"]),
        made_call=False,
    )

    assert await _recover(host) == 1
    assert host.failures == []
    assert len(host.reprompts) == 1


@pytest.mark.asyncio
async def test_mcp_call_lookup_failure_fails_open_to_reprompts() -> None:
    host = _FakeHost(step_context=_step_context(_MCP_ONLY_TOOLS), made_call=None)

    assert await _recover(host) == 1
    assert host.failures == []
    assert len(host.reprompts) == 1


def test_step_requires_gobby_proxy_boundaries() -> None:
    assert step_requires_gobby_proxy(None) is False
    assert step_requires_gobby_proxy(_step_context("all")) is False
    assert step_requires_gobby_proxy(_step_context([])) is False
    assert step_requires_gobby_proxy(_step_context(_MCP_ONLY_TOOLS)) is True
    assert step_requires_gobby_proxy(_step_context(["Bash", "mcp__gobby__call_tool"])) is False
