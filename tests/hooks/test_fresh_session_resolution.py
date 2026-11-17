"""Fresh provider boundaries take precedence over pending handoff markers."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start.handoff import resolve_session_start_identity
from gobby.hooks.events import HookEventType, HookResponse
from gobby.storage.sessions._update_sentinel import UNSET
from tests.hooks._event_handler_helpers import TEST_MACHINE_ID, make_event


@pytest.mark.unit
@pytest.mark.parametrize("source", ["new", "startup"])
@pytest.mark.parametrize("provider_binding", ["unbound", "same", "previous"])
def test_launch_hint_only_adopts_the_current_or_unbound_session(
    source: str,
    provider_binding: str,
    event_handlers: EventHandlers,
    mock_dependencies: dict[str, Any],
    mock_empty_session_variable_manager: MagicMock,
) -> None:
    manager = mock_dependencies["session_manager"]
    old = SimpleNamespace(
        id="managed-session",
        agent_run_id=None,
        external_id={
            "unbound": "managed-session",
            "same": "new-provider-id",
            "previous": "old-provider-id",
        }[provider_binding],
        machine_id=TEST_MACHINE_ID,
        status="active",
        project_id="project-id",
        session_type="terminal",
    )
    manager.get.side_effect = lambda session_id: old if session_id == old.id else None
    manager.find_by_external_id.return_value = None
    manager.find_by_external_id_any_project.return_value = None
    manager.register_session.return_value = "fresh-session"
    event = make_event(
        HookEventType.SESSION_START,
        session_id="new-provider-id",
        data={"source": source, "terminal_context": {"gobby_session_id": old.id}},
    )
    with (
        patch(
            "gobby.hooks.event_handlers._session_start.flow.session_start_should_defer",
            return_value=False,
        ),
        patch.object(event_handlers, "_activate_materialized_session", return_value=[]),
        patch.object(
            event_handlers, "_handle_pre_created_session", return_value=HookResponse()
        ) as adopt,
    ):
        response = event_handlers.handle_session_start(event)
    assert response.decision == "allow"
    if provider_binding != "previous":
        adopt.assert_called_once()
        manager.register_session.assert_not_called()
    else:
        adopt.assert_not_called()
        manager.register_session.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("source", ["clear", "new", "startup", "resume"])
def test_fresh_registration_discards_launch_authority(
    source: str,
    event_handlers: EventHandlers,
    mock_dependencies: dict[str, Any],
    mock_empty_session_variable_manager: MagicMock,
) -> None:
    manager = mock_dependencies["session_manager"]
    manager.get.return_value = None
    manager.find_by_external_id.return_value = None
    manager.find_by_external_id_any_project.return_value = None
    manager.register_session.return_value = "fresh-session"
    event = make_event(
        HookEventType.SESSION_START,
        session_id="new-provider-id",
        data={
            "source": source,
            "parent_session_id": "old-parent",
            "workflow_name": "synthetic-workflow",
            "agent_depth": 3,
        },
    )
    with (
        patch(
            "gobby.hooks.event_handlers._session_start.flow.session_start_should_defer",
            return_value=False,
        ),
        patch.object(event_handlers, "_activate_materialized_session", return_value=[]),
    ):
        response = event_handlers.handle_session_start(event)
    assert response.decision == "allow"
    registration = manager.register_session.call_args.kwargs
    assert registration["parent_session_id"] is UNSET
    assert registration["workflow_name"] is None
    assert registration["agent_depth"] == 0


@pytest.mark.unit
@pytest.mark.parametrize("source", ["clear", "new", "startup"])
def test_fresh_boundary_does_not_promote_pending_handoff(source: str) -> None:
    sessions = MagicMock()
    sessions.find_by_external_id.return_value = None
    sessions.find_by_external_id_any_project.return_value = None
    predecessor = SimpleNamespace(id="old-session", machine_id="machine")
    handler = SimpleNamespace(_session_manager=sessions, logger=MagicMock())
    payload: dict[str, object] = {"source": source}
    with (
        patch(
            "gobby.hooks.event_handlers._session_start.handoff.resolve_clear_continuation",
            create=True,
            return_value=SimpleNamespace(
                predecessor=predecessor,
                attempt_id="old-clear",
                degrade_reason=None,
                supersedes=None,
            ),
        ) as resolve_clear,
        patch(
            "gobby.hooks.event_handlers._session_start.handoff.resolve_compact_continuation",
            return_value=SimpleNamespace(
                session=predecessor,
                ambiguous=False,
                conflicting_session_ids=(),
            ),
        ) as resolve_compact,
    ):
        resolution = resolve_session_start_identity(
            handler,
            payload,
            source,
            external_id="fresh-provider-id",
            machine_id="machine",
            project_id="project",
            cli_source="codex",
            terminal_context={"parent_pid": 123, "parent_create_time": 42.0},
        )

    assert resolution.session is None
    assert resolution.session_source == source
    assert payload["source"] == source
    resolve_clear.assert_not_called()
    resolve_compact.assert_not_called()
