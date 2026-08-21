from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import resume_finalization
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.recovery_state import is_daemon_stop_parked
from gobby.agents.spawn_executor_support import _session_manager_validation_error
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.terminal_prompt_monitor import _is_expected_prompt_probe_error
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_lifecycle_callbacks_fail_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = cast(Any, object.__new__(AgentLifecycleMonitor))
    monitor._running = True
    monitor._check_interval = 0
    monitor._reconciliation_callback = AsyncMock(side_effect=RuntimeError("reconcile failed"))
    monitor._non_task_resume_callback = AsyncMock()
    check_names = (
        "reconcile_pending_terminations",
        "check_trust_prompts",
        "check_loop_prompts",
        "check_approval_prompts",
        "check_queued_continuation_prompts",
        "check_periodic_enters",
        "check_attention_agents",
        "check_unhealthy_agents",
        "check_agent_memory",
        "reap_daemon_stop_orphans",
        "expire_terminal_run_sessions",
        "check_initialization_timeout",
        "check_idle_agents",
        "check_provider_stalls",
        "check_autonomous_stuck_agents",
        "refresh_active_run_dispatch_mutexes",
    )
    checks = {name: AsyncMock(return_value=0) for name in check_names}
    for name, check in checks.items():
        setattr(monitor, name, check)

    async def stop_after_first_check() -> int:
        monitor._running = False
        return 0

    checks["reconcile_pending_terminations"].side_effect = stop_after_first_check
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await monitor._check_loop()

    assert monitor._running is False
    assert all(check.await_count == 1 for check in checks.values())
    monitor._non_task_resume_callback.assert_awaited_once_with()
    checks["check_trust_prompts"].assert_awaited_once_with()


def test_daemon_stop_run_owned_by_orphan_reaper_is_not_parked() -> None:
    run = SimpleNamespace(
        status="cancelled",
        terminal_reason="daemon_stop",
        resume_metadata_json={"daemon_stop_orphan_reap_started_at": "2026-07-28T00:00:00Z"},
    )

    assert is_daemon_stop_parked(run) is False


def test_missing_tmux_executable_is_not_a_vanished_target() -> None:
    missing_executable = FileNotFoundError(2, "No such file or directory", "tmux")
    missing_socket = FileNotFoundError(
        2,
        "No such file or directory",
        "/tmp/tmux-501/gobby.sock",
    )

    assert _is_expected_prompt_probe_error(missing_executable) is False
    assert _is_expected_prompt_probe_error(missing_socket) is True


def test_session_manager_requires_sandbox_policy_hash_writer() -> None:
    manager = SimpleNamespace(
        _storage=SimpleNamespace(db=object()),
        create_child_session=lambda *args, **kwargs: None,
        update_terminal_pickup_metadata=lambda *args, **kwargs: None,
        update_sandbox_enabled=lambda *args, **kwargs: None,
    )
    request = SpawnRequest(
        prompt="test",
        cwd="/repo",
        provider="codex",
        session_id="session",
        run_id="run",
        parent_session_id="parent",
        project_id="project",
        session_manager=cast(Any, manager),
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )

    result = _session_manager_validation_error(request, "codex")

    assert result is not None
    assert "update_sandbox_policy_hash" in (result.error or "")


def test_recovery_notification_dedupe_uses_one_database_identity() -> None:
    cursor = MagicMock()
    cursor.fetchone.side_effect = [{"id": "created"}, None]
    db = MagicMock()
    db.execute.return_value = cursor

    first = resume_finalization.notify_parent_of_recovery(
        db,
        child_session_id="11111111-1111-1111-1111-111111111111",
        parent_session_id="22222222-2222-2222-2222-222222222222",
        content="Recovered",
        run_id="run",
        event="resume",
        dedupe_key="boot",
    )
    second = resume_finalization.notify_parent_of_recovery(
        db,
        child_session_id="11111111-1111-1111-1111-111111111111",
        parent_session_id="22222222-2222-2222-2222-222222222222",
        content="Recovered",
        run_id="run",
        event="resume",
        dedupe_key="boot",
    )

    assert first is True
    assert second is False
    first_params = db.execute.call_args_list[0].args[1]
    second_params = db.execute.call_args_list[1].args[1]
    assert first_params[0] == second_params[0]
    assert "ON CONFLICT (id) DO NOTHING" in db.execute.call_args_list[0].args[0]


def test_resume_finalization_tolerates_stopped_registry_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(successor_run_id="successor")
    monkeypatch.setattr(
        resume_finalization,
        "finalize_daemon_resume",
        lambda *args, **kwargs: result,
    )
    registry_loop = MagicMock()
    registry_loop.is_running.return_value = True
    registry_loop.call_soon_threadsafe.side_effect = RuntimeError("loop stopped")

    actual = resume_finalization.finalize_resume_handoff_threadsafe(
        MagicMock(),
        original_run_id="original",
        successor_run_id="successor",
        child_session_id="child",
        completion_registry=MagicMock(),
        registry_loop=registry_loop,
    )

    assert actual.successor_run_id == "successor"
    assert registry_loop.call_soon_threadsafe.call_count == 1


def test_resume_finalization_propagates_registry_callback_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(successor_run_id="successor")
    monkeypatch.setattr(
        resume_finalization,
        "finalize_daemon_resume",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        resume_finalization,
        "reconcile_completion_registry",
        MagicMock(side_effect=RuntimeError("registry bug")),
    )
    registry_loop = MagicMock()
    registry_loop.is_running.return_value = True
    registry_loop.call_soon_threadsafe.side_effect = lambda callback: callback()

    with pytest.raises(RuntimeError, match="registry bug"):
        resume_finalization.finalize_resume_handoff_threadsafe(
            MagicMock(),
            original_run_id="original",
            successor_run_id="successor",
            child_session_id="child",
            completion_registry=MagicMock(),
            registry_loop=registry_loop,
        )
    assert registry_loop.call_soon_threadsafe.call_count == 1
