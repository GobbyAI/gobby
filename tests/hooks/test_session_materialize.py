"""Deferred first-activity materialization."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.session_materialize import activate_deferred_session

pytestmark = pytest.mark.unit

_DERIVED = "/home/user/.grok/sessions/%2Frepo/grok-external/updates.jsonl"


def _manager(session: SimpleNamespace, updated: SimpleNamespace | None) -> MagicMock:
    manager = MagicMock()
    manager._session_manager.get.return_value = session
    manager._session_manager.update.return_value = updated
    manager._event_handlers._derive_transcript_path.return_value = _DERIVED
    manager._event_handlers._activate_materialized_session.return_value = []
    manager._event_handlers._compose_session_response.return_value = HookResponse(
        decision="allow",
        system_message="Gobby Session ID: #7",
    )
    manager._evaluate_workflow_rules.return_value = (None, None)
    manager._evaluate_blocking_webhooks.return_value = None
    manager.get_machine_id.return_value = "machine-1"
    return manager


def _event(data: dict[str, object]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="grok-external",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data=data,
        machine_id="machine-1",
        project_id="project-1",
        metadata={"_platform_session_id": "platform-session"},
    )


def test_deferred_grok_session_derives_and_persists_transcript_path() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path=None,
    )
    updated = SimpleNamespace(**{**vars(session), "transcript_path": _DERIVED})
    manager = _manager(session, updated)
    event = _event({"prompt": "hello", "cwd": "/repo"})

    assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    manager._event_handlers._derive_transcript_path.assert_called_once_with(
        "grok",
        event.data,
        "grok-external",
        owner_machine_id="machine-1",
        local_machine_id="machine-1",
    )
    manager._session_manager.update.assert_called_once_with(
        session_id="platform-session",
        transcript_path=_DERIVED,
    )
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["transcript_path"] == _DERIVED
    assert activate["session_obj"] is updated


def test_native_transcript_path_skips_derivation() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path=None,
    )
    manager = _manager(session, None)
    event = _event({"prompt": "hello", "cwd": "/repo", "transcript_path": "/repo/t.jsonl"})

    assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    manager._event_handlers._derive_transcript_path.assert_not_called()
    manager._session_manager.update.assert_not_called()
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["transcript_path"] == "/repo/t.jsonl"
