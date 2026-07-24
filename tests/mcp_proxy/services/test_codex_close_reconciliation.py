from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.mcp_proxy.services import result_handling
from gobby.mcp_proxy.services.result_handling import (
    apply_before_tool_enforcement,
    build_before_tool_event,
)
from gobby.workflows.condition_helpers import completion_evidence_ready
from gobby.workflows.observer_verification import detect_verification_evidence

pytestmark = pytest.mark.unit


def test_codex_close_reconciliation_default_allows_large_receipt_batches() -> None:
    assert result_handling._CODEX_RECONCILE_TIMEOUT_SECONDS == 60.0


class _Processor:
    def __init__(
        self,
        order: list[str],
        variables: dict[str, Any],
        shell_exit_code: int | None,
    ) -> None:
        self.order = order
        self.variables = variables
        self.shell_exit_code = shell_exit_code
        self.session_ids: list[str] = []

    async def reconcile_codex_transcript(self, session_id: str) -> SimpleNamespace:
        self.order.append("reconcile")
        self.session_ids.append(session_id)
        if self.shell_exit_code is not None:
            data: dict[str, Any] = {
                "tool_name": "Bash",
                "tool_input": {"command": "uv run pytest tests/workflows -q"},
                "tool_output": {"exit_code": self.shell_exit_code, "output": "focused"},
            }
            normalize_tool_fields(data)
            detect_verification_evidence(
                HookEvent(
                    event_type=HookEventType.AFTER_TOOL,
                    session_id=session_id,
                    source=SessionSource.CODEX,
                    timestamp=datetime.now(UTC),
                    data=data,
                ),
                self.variables,
                session_id,
            )
        return SimpleNamespace(flushed=True, error=None)


class _WorkflowHandler:
    def __init__(self, order: list[str], variables: dict[str, Any], require_ready: bool) -> None:
        self.order = order
        self.variables = variables
        self.require_ready = require_ready

    def evaluate(self, _event: HookEvent) -> HookResponse:
        self.order.append("evaluate")
        if self.require_ready and not completion_evidence_ready(self.variables):
            return HookResponse(decision="block", reason="completion readiness blocked")
        return HookResponse(decision="allow")


class _Service:
    def __init__(
        self,
        source: SessionSource,
        *,
        shell_exit_code: int | None = None,
        require_ready: bool = False,
    ) -> None:
        self.order: list[str] = []
        self.variables: dict[str, Any] = {}
        self.processor = _Processor(self.order, self.variables, shell_exit_code)
        self.hook_manager = SimpleNamespace(
            _workflow_handler=_WorkflowHandler(self.order, self.variables, require_ready),
            _message_processor=self.processor,
            event_handlers=None,
        )
        self.source = source

    def _get_effective_session_id(self, session_id: str | None) -> str | None:
        return session_id

    def _resolve_hook_manager(self) -> Any:
        return self.hook_manager

    def _resolve_tool_event_context(self, _session_id: str) -> tuple[Any, ...]:
        return (
            self.hook_manager,
            None,
            None,
            self.source,
            {"_platform_session_id": "platform-codex-session"},
            None,
            None,
        )

    def _build_before_tool_event(
        self,
        *,
        effective_session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> HookEvent:
        return build_before_tool_event(
            self,
            effective_session_id,
            server_name,
            tool_name,
            arguments,
        )

    def _prepare_arguments(
        self, arguments: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return arguments if isinstance(arguments, dict) else {}, None


@pytest.mark.asyncio
async def test_codex_close_reconciles_transcript_before_completion_rules() -> None:
    service = _Service(SessionSource.CODEX, shell_exit_code=0, require_ready=True)

    _, _, _, error, _ = await apply_before_tool_enforcement(
        service,
        "gobby-tasks",
        "close_task",
        {"task_id": "task-1", "commit_sha": "abc123"},
        "external-codex-session",
    )

    assert error is None
    assert service.order == ["reconcile", "evaluate"]
    assert service.processor.session_ids == ["platform-codex-session"]
    assert service.variables["verification_evidence_recorded"] is True


@pytest.mark.asyncio
async def test_codex_close_stays_blocked_when_reconciled_result_failed() -> None:
    service = _Service(SessionSource.CODEX, shell_exit_code=1, require_ready=True)

    _, _, _, error, _ = await apply_before_tool_enforcement(
        service,
        "gobby-tasks",
        "close_task",
        {"task_id": "task-1", "commit_sha": "abc123"},
        "external-codex-session",
    )

    assert error is not None
    assert error["error"] == "completion readiness blocked"
    assert service.order == ["reconcile", "evaluate"]
    assert service.variables["verification_evidence_recorded"] is False


@pytest.mark.asyncio
async def test_codex_close_reconciliation_timeout_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service(SessionSource.CODEX, require_ready=True)
    service.variables["verification_evidence_recorded"] = True
    release = asyncio.Event()

    async def _slow_reconcile(_session_id: str) -> SimpleNamespace:
        service.order.append("reconcile")
        await release.wait()
        return SimpleNamespace(flushed=True, error=None)

    service.hook_manager._message_processor = SimpleNamespace(
        reconcile_codex_transcript=_slow_reconcile
    )
    monkeypatch.setattr(result_handling, "_CODEX_RECONCILE_TIMEOUT_SECONDS", 0.01)

    _, _, _, error, _ = await apply_before_tool_enforcement(
        service,
        "gobby-tasks",
        "close_task",
        {"task_id": "task-1", "commit_sha": "abc123"},
        "external-codex-session",
    )

    assert error is not None
    assert error["error_code"] == "TOOL_BLOCKED"
    assert error["retryable"] is True
    assert "retry task closure" in error["error"]
    assert service.order == ["reconcile"]
    background = result_handling._CODEX_RECONCILE_TASKS["platform-codex-session"]
    release.set()
    await background


async def test_codex_close_retry_joins_timed_out_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service(SessionSource.CODEX, require_ready=False)
    release = asyncio.Event()
    cancelled = False
    reconcile_calls = 0

    async def _slow_reconcile(_session_id: str) -> SimpleNamespace:
        nonlocal cancelled, reconcile_calls
        reconcile_calls += 1
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return SimpleNamespace(flushed=True, error=None)

    service.hook_manager._message_processor = SimpleNamespace(
        reconcile_codex_transcript=_slow_reconcile
    )
    monkeypatch.setattr(result_handling, "_CODEX_RECONCILE_TIMEOUT_SECONDS", 0.01)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="platform-codex-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": "platform-codex-session"},
    )
    first_result = await result_handling._reconcile_codex_close_transcript(
        service.hook_manager,
        event,
        server_name="gobby-tasks",
        tool_name="close_task",
        effective_session_id="platform-codex-session",
    )
    asyncio.get_running_loop().call_soon(release.set)
    retry_result = await result_handling._reconcile_codex_close_transcript(
        service.hook_manager,
        event,
        server_name="gobby-tasks",
        tool_name="close_task",
        effective_session_id="platform-codex-session",
    )

    assert first_result is False
    assert retry_result is True
    assert reconcile_calls == 1
    assert cancelled is False


@pytest.mark.asyncio
async def test_codex_close_reconciliation_os_error_remains_fail_closed() -> None:
    async def _failed_reconcile(_session_id: str) -> SimpleNamespace:
        raise OSError("transcript unavailable")

    event = SimpleNamespace(source=SessionSource.CODEX, metadata={})
    hook_manager = SimpleNamespace(
        _message_processor=SimpleNamespace(reconcile_codex_transcript=_failed_reconcile)
    )

    reconciled = await result_handling._reconcile_codex_close_transcript(
        hook_manager,
        event,
        server_name="gobby-tasks",
        tool_name="close_task",
        effective_session_id="session-1",
    )

    assert reconciled is False


@pytest.mark.asyncio
async def test_codex_close_reconciliation_unexpected_error_propagates() -> None:
    async def _buggy_reconcile(_session_id: str) -> SimpleNamespace:
        raise RuntimeError("implementation bug")

    event = SimpleNamespace(source=SessionSource.CODEX, metadata={})
    hook_manager = SimpleNamespace(
        _message_processor=SimpleNamespace(reconcile_codex_transcript=_buggy_reconcile)
    )

    with pytest.raises(RuntimeError, match="implementation bug"):
        await result_handling._reconcile_codex_close_transcript(
            hook_manager,
            event,
            server_name="gobby-tasks",
            tool_name="close_task",
            effective_session_id="session-1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "server_name", "tool_name"),
    [
        (SessionSource.CLAUDE, "gobby-tasks", "close_task"),
        (SessionSource.CODEX, "gobby-tasks", "claim_task"),
        (SessionSource.CODEX, "gobby-memory", "create_memory"),
    ],
)
async def test_non_codex_close_calls_do_not_reconcile(
    source: SessionSource,
    server_name: str,
    tool_name: str,
) -> None:
    service = _Service(source)

    _, _, _, error, _ = await apply_before_tool_enforcement(
        service,
        server_name,
        tool_name,
        {},
        "external-session",
    )

    assert error is None
    assert service.order == ["evaluate"]
    assert service.processor.session_ids == []
