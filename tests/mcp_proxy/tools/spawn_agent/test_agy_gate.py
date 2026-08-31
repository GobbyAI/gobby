"""AGY support gate runs before any spawn side effect (plan row 6.1.7)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.spawn_executor import _spawn_agy_terminal
from gobby.agents.spawn_executor_providers import agy_support_refusal
from gobby.agents.spawn_models import SpawnRequest
from gobby.mcp_proxy.tools.spawn_agent import _implementation
from gobby.providers.version_gate import (
    AGY_REQUIRED_VERSION,
    AGY_REVALIDATING_REASON,
    AGY_UNPUBLISHED_REASON,
    AgySupportRecord,
)
from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


def _record(installed_version: str | None, *, supported: bool, reason: str) -> AgySupportRecord:
    return AgySupportRecord(
        installed_version=installed_version,
        required_version=AGY_REQUIRED_VERSION,
        supported=supported,
        reason=reason,
        identity=None,
    )


_MISSING_BINARY = _record(
    None,
    supported=False,
    reason=f"Installed AGY version none does not meet required version {AGY_REQUIRED_VERSION}.",
)
_SUB_FLOOR = _record(
    "1.1.0",
    supported=False,
    reason=f"Installed AGY version 1.1.0 does not meet required version {AGY_REQUIRED_VERSION}.",
)
_UNPARSEABLE = _record(
    None,
    supported=False,
    reason=(
        f"Installed AGY version unparseable does not meet required version {AGY_REQUIRED_VERSION}."
    ),
)
_UNPUBLISHED = _record(None, supported=False, reason=AGY_UNPUBLISHED_REASON)
_REVALIDATING = _record(None, supported=False, reason=AGY_REVALIDATING_REASON)
_SUPPORTED = _record(
    AGY_REQUIRED_VERSION,
    supported=True,
    reason=f"AGY {AGY_REQUIRED_VERSION} meets required version {AGY_REQUIRED_VERSION}.",
)
_REFUSED_RECORDS = [_MISSING_BINARY, _SUB_FLOOR, _UNPARSEABLE, _UNPUBLISHED, _REVALIDATING]
_REFUSED_IDS = ["missing-binary", "sub-floor", "unparseable", "unpublished", "revalidating"]


def _agent_body(provider: str) -> AgentDefinitionBody:
    return AgentDefinitionBody(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name="default",
        provider=provider,
    )


def _session_manager(sources: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(get=lambda session_id: SimpleNamespace(source=sources.get(session_id)))


_SELECTION_PATHS: dict[str, dict[str, Any]] = {
    "explicit": {
        "provider": "agy",
        "agent_body": _agent_body("claude"),
        "caller_session_id": None,
        "sources": {"parent": "codex"},
    },
    "inherited": {
        "provider": "inherit",
        "agent_body": _agent_body("inherit"),
        "caller_session_id": "caller",
        "sources": {"caller": "agy", "parent": "claude"},
    },
    "agent-configured": {
        "provider": None,
        "agent_body": _agent_body("agy"),
        "caller_session_id": None,
        "sources": {"parent": "claude"},
    },
    "default": {
        "provider": None,
        "agent_body": None,
        "caller_session_id": None,
        "sources": {"parent": "agy"},
    },
}


class _SideEffects:
    """Every spawn side effect, patched so any call is a test failure."""

    def __init__(self) -> None:
        self.runner = MagicMock()
        self.runner.can_spawn.return_value = (True, "ok", 0)
        self.worktree_storage = MagicMock()
        self.git_manager = MagicMock()
        self.patches = {
            name: patch.object(_implementation, name)
            for name in (
                "get_project_context",
                "get_isolation_handler",
                "reserve_agent_slot",
                "prepare_terminal_spawn",
                "execute_spawn",
            )
        }
        self.mocks: dict[str, MagicMock] = {}

    def __enter__(self) -> _SideEffects:
        self.mocks = {name: patcher.start() for name, patcher in self.patches.items()}
        return self

    def __exit__(self, *exc: object) -> None:
        for patcher in self.patches.values():
            patcher.stop()

    def assert_none_happened(self) -> None:
        for name, mock in self.mocks.items():
            assert not mock.called, f"{name} ran despite the AGY gate refusing"
        assert self.worktree_storage.mock_calls == []
        assert self.git_manager.mock_calls == []
        assert self.runner.child_session_manager.mock_calls == []
        assert self.runner.run_storage.mock_calls == []


async def _spawn(effects: _SideEffects, selection: dict[str, Any]) -> dict[str, Any]:
    return await _implementation.spawn_agent_impl(
        prompt="do the work",
        runner=effects.runner,
        agent_body=selection["agent_body"],
        provider=selection["provider"],
        parent_session_id="parent",
        caller_session_id=selection["caller_session_id"],
        session_manager=_session_manager(selection["sources"]),
        isolation="worktree",
        worktree_storage=effects.worktree_storage,
        git_manager=effects.git_manager,
        terminal_backend="tmux",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("record", _REFUSED_RECORDS, ids=_REFUSED_IDS)
@pytest.mark.parametrize("path", sorted(_SELECTION_PATHS))
async def test_agy_gate_refuses_before_any_side_effect(path: str, record: AgySupportRecord) -> None:
    with (
        _SideEffects() as effects,
        patch.object(_implementation, "peek_agy_support", return_value=record) as peek,
    ):
        result = await _spawn(effects, _SELECTION_PATHS[path])

    assert result == {"success": False, "error": agy_support_refusal(record)}
    assert result["error"] == record.reason
    peek.assert_called_once_with()
    effects.assert_none_happened()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(_SELECTION_PATHS))
async def test_supported_record_passes_the_gate(path: str) -> None:
    with (
        _SideEffects() as effects,
        patch.object(_implementation, "peek_agy_support", return_value=_SUPPORTED) as peek,
    ):
        effects.mocks["get_project_context"].return_value = None
        result = await _spawn(effects, _SELECTION_PATHS[path])

    peek.assert_called_once_with()
    effects.mocks["get_project_context"].assert_called_once()
    assert result == {"success": False, "error": "Could not resolve project context"}


@pytest.mark.asyncio
async def test_non_agy_providers_never_consult_the_gate() -> None:
    with (
        _SideEffects() as effects,
        patch.object(_implementation, "peek_agy_support") as peek,
    ):
        effects.mocks["get_project_context"].return_value = None
        result = await _spawn(effects, {**_SELECTION_PATHS["explicit"], "provider": "codex"})

    peek.assert_not_called()
    assert result == {"success": False, "error": "Could not resolve project context"}


@pytest.mark.asyncio
@pytest.mark.parametrize("record", _REFUSED_RECORDS, ids=_REFUSED_IDS)
async def test_gate_and_terminal_executor_emit_one_refusal_message(
    record: AgySupportRecord,
) -> None:
    with (
        _SideEffects() as effects,
        patch.object(_implementation, "peek_agy_support", return_value=record),
    ):
        gate_result = await _spawn(effects, _SELECTION_PATHS["explicit"])

    request = SpawnRequest(
        prompt="do the work",
        cwd="/repo",
        provider="agy",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    with (
        patch("gobby.providers.version_gate.ensure_agy_support", AsyncMock(return_value=record)),
        patch("gobby.agents.spawn_executor._runtime_spawn", AsyncMock()) as runtime_spawn,
    ):
        executor_result = await _spawn_agy_terminal(request)

    assert executor_result.success is False
    assert executor_result.error == gate_result["error"] == agy_support_refusal(record)
    runtime_spawn.assert_not_awaited()
