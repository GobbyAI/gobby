from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.hooks.agent_run_ingress import (
    TERMINAL_INGRESS_HOOK_TYPES,
    AgentRunIngressRetryableError,
    validate_managed_agent_hook,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource

_SESSION_ID = "d92fc5be-6638-415d-8143-c349293fb35c"
_RUN_ID = "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764"
_ORIGINAL_RUN_ID = "df921043-37e8-436b-a239-57a0ee3284bd"


def _event(run_id: str | None) -> HookEvent:
    terminal_context = {} if run_id is None else {"gobby_agent_run_id": run_id}
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="external-child",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"terminal_context": terminal_context},
        metadata={"_platform_session_id": _SESSION_ID},
    )


def _managers(*, phase: str | None = None) -> tuple[MagicMock, MagicMock]:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(agent_run_id=_RUN_ID)
    run_manager = MagicMock()
    metadata = (
        {
            "daemon_stop_resume_phase": phase,
            "resumed_from_run_id": _ORIGINAL_RUN_ID,
        }
        if phase
        else {}
    )
    run_manager.get.return_value = SimpleNamespace(
        id=_RUN_ID,
        resume_metadata_json=metadata,
    )
    return session_manager, run_manager


def test_missing_managed_run_identity_is_retryable() -> None:
    session_manager, run_manager = _managers()

    with pytest.raises(AgentRunIngressRetryableError) as exc_info:
        validate_managed_agent_hook(
            _event(None),
            session_manager=session_manager,
            agent_run_manager=run_manager,
            database=MagicMock(),
            completion_registry=None,
            registry_loop=None,
        )

    assert exc_info.value.expected_run_id == _RUN_ID
    run_manager.get.assert_not_called()


def test_stale_managed_run_identity_is_acknowledged_without_side_effects() -> None:
    session_manager, run_manager = _managers()

    result = validate_managed_agent_hook(
        _event("8292b975-f848-42c4-a8a9-6b6e8b30ddc6"),
        session_manager=session_manager,
        agent_run_manager=run_manager,
        database=MagicMock(),
        completion_registry=None,
        registry_loop=None,
    )

    assert result.accepted is False
    assert result.managed is True
    run_manager.get.assert_not_called()


def test_matching_hook_finalizes_provisional_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager, run_manager = _managers(phase="runtime_persisted")
    finalize = MagicMock()
    monkeypatch.setattr(
        "gobby.hooks.agent_run_ingress.finalize_resume_handoff_threadsafe",
        finalize,
    )
    database = MagicMock()
    registry = MagicMock()

    result = validate_managed_agent_hook(
        _event(_RUN_ID),
        session_manager=session_manager,
        agent_run_manager=run_manager,
        database=database,
        completion_registry=registry,
        registry_loop=None,
    )

    assert result.accepted is True
    assert result.managed is True
    assert result.run_id == _RUN_ID
    finalize.assert_called_once_with(
        database,
        original_run_id=_ORIGINAL_RUN_ID,
        successor_run_id=_RUN_ID,
        child_session_id=_SESSION_ID,
        completion_registry=registry,
        registry_loop=None,
    )
    assert finalize.call_count == 1


def test_terminal_ingress_hook_types_cover_exactly_the_terminal_hooks() -> None:
    assert TERMINAL_INGRESS_HOOK_TYPES == frozenset(
        {
            HookEventType.SESSION_END,
            HookEventType.STOP,
            HookEventType.AFTER_AGENT,
        }
    )


def test_session_without_run_owner_is_accepted_as_unmanaged() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(agent_run_id=None)
    run_manager = MagicMock()

    result = validate_managed_agent_hook(
        _event(_RUN_ID),
        session_manager=session_manager,
        agent_run_manager=run_manager,
        database=MagicMock(),
        completion_registry=None,
        registry_loop=None,
    )

    assert result.accepted is True
    assert result.managed is False
    run_manager.get.assert_not_called()


def test_non_uuid_managed_run_identity_is_retryable() -> None:
    session_manager, run_manager = _managers()

    with pytest.raises(AgentRunIngressRetryableError) as exc_info:
        validate_managed_agent_hook(
            _event("not-a-uuid"),
            session_manager=session_manager,
            agent_run_manager=run_manager,
            database=MagicMock(),
            completion_registry=None,
            registry_loop=None,
        )

    assert exc_info.value.expected_run_id == _RUN_ID
    run_manager.get.assert_not_called()


def test_matching_hook_without_durable_run_is_retryable() -> None:
    session_manager, run_manager = _managers()
    run_manager.get.return_value = None

    with pytest.raises(AgentRunIngressRetryableError) as exc_info:
        validate_managed_agent_hook(
            _event(_RUN_ID),
            session_manager=session_manager,
            agent_run_manager=run_manager,
            database=MagicMock(),
            completion_registry=None,
            registry_loop=None,
        )

    assert exc_info.value.expected_run_id == _RUN_ID
    assert exc_info.value.reason == "managed hook run is not durable yet"


def test_matching_hook_without_provisional_phase_skips_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager, run_manager = _managers()
    finalize = MagicMock()
    monkeypatch.setattr(
        "gobby.hooks.agent_run_ingress.finalize_resume_handoff_threadsafe",
        finalize,
    )

    result = validate_managed_agent_hook(
        _event(_RUN_ID),
        session_manager=session_manager,
        agent_run_manager=run_manager,
        database=MagicMock(),
        completion_registry=None,
        registry_loop=None,
    )

    assert result.accepted is True
    assert result.managed is True
    assert result.run_id == _RUN_ID
    finalize.assert_not_called()


def test_provisional_finalization_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager, run_manager = _managers(phase="launch_requested")
    finalize = MagicMock(side_effect=RuntimeError("finalize failed"))
    monkeypatch.setattr(
        "gobby.hooks.agent_run_ingress.finalize_resume_handoff_threadsafe",
        finalize,
    )

    with pytest.raises(AgentRunIngressRetryableError) as exc_info:
        validate_managed_agent_hook(
            _event(_RUN_ID),
            session_manager=session_manager,
            agent_run_manager=run_manager,
            database=MagicMock(),
            completion_registry=None,
            registry_loop=None,
        )

    assert exc_info.value.expected_run_id == _RUN_ID
    assert exc_info.value.reason == "provisional resume finalization failed: RuntimeError"
    finalize.assert_called_once()


def test_provisional_resume_missing_original_run_identity_is_retryable() -> None:
    session_manager, run_manager = _managers(phase="runtime_persisted")
    run_manager.get.return_value = SimpleNamespace(
        id=_RUN_ID,
        resume_metadata_json={"daemon_stop_resume_phase": "runtime_persisted"},
    )

    with pytest.raises(AgentRunIngressRetryableError) as exc_info:
        validate_managed_agent_hook(
            _event(_RUN_ID),
            session_manager=session_manager,
            agent_run_manager=run_manager,
            database=MagicMock(),
            completion_registry=None,
            registry_loop=None,
        )

    assert exc_info.value.expected_run_id == _RUN_ID
    assert exc_info.value.reason == "provisional resume is missing its original run identity"
