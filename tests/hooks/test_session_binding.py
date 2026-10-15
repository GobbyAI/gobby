"""A hook from another machine binds only that machine's session rows."""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType, HookResponse
from gobby.hooks.project_context import resolve_hook_project_context
from gobby.hooks.session_binding import session_binds_event
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.terminal_context import enrich_terminal_context_with_cwd
from gobby.sessions.compact_identity import CompactIdentityResolution
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit

HUB = "21000000-0000-4000-8000-0000000000a1"
NODE = "21000000-0000-4000-8000-0000000000a2"
OTHER = "21000000-0000-4000-8000-0000000000a3"
FOREIGN_ID = "foreign-session"
NODE_CWD = "/node/repo"
_HANDOFF = "gobby.hooks.event_handlers._session_start.handoff"
_FLOW = "gobby.hooks.event_handlers._session_start.flow"


@pytest.fixture(autouse=True)
def _hub_machine() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", HUB):
        yield


def _row(machine_id: str | None, **fields: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": FOREIGN_ID,
        "external_id": "foreign-external",
        "machine_id": machine_id,
        "project_id": "foreign-project",
        "source": "claude",
        "session_type": "terminal",
        "status": "active",
        "terminal_context": None,
        "transcript_path": None,
        "title": "Foreign",
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _refuse_foreign_registration(**kwargs: Any) -> str:
    """Model register_session's local-machine refusal with the real guard."""
    require_local_machine_id(
        kwargs.get("machine_id"), resource_kind="session", resource_id=kwargs["external_id"]
    )
    return "registered-session"


@pytest.mark.parametrize(
    ("row_machine", "event_machine", "binds"),
    [
        (OTHER, None, True),
        (OTHER, "", True),
        (OTHER, HUB, True),
        (None, HUB, True),
        (NODE, NODE, True),
        (OTHER, NODE, False),
        (HUB, NODE, False),
        (None, NODE, False),
        ("", NODE, False),
    ],
)
def test_binding_predicate(row_machine: str | None, event_machine: str | None, binds: bool) -> None:
    assert session_binds_event(row_machine, event_machine) is binds


def _start_handlers(
    mock_dependencies: dict[str, Any],
) -> tuple[EventHandlers, MagicMock]:
    manager: MagicMock = mock_dependencies["session_manager"]
    manager.register_session.side_effect = _refuse_foreign_registration
    handlers = EventHandlers(**mock_dependencies, resolve_project_id=lambda p, c: "node-project")
    return handlers, manager


_NO_CLEAR = SimpleNamespace(predecessor=None, attempt_id=None, degrade_reason=None, supersedes=None)


@pytest.mark.parametrize("path", ["external_id", "terminal_context", "web_chat", "compact"])
def test_session_start_skips_foreign_rows(
    mock_dependencies: dict[str, Any],
    mock_empty_session_variable_manager: MagicMock,
    path: str,
) -> None:
    handlers, manager = _start_handlers(mock_dependencies)
    foreign = _row(OTHER, session_type="web_chat" if path == "web_chat" else "terminal")
    data: dict[str, Any] = {"source": "resume", "cwd": NODE_CWD}
    if path == "terminal_context":
        data["terminal_context"] = {"gobby_session_id": FOREIGN_ID}
    manager.get.side_effect = lambda ref: (
        foreign
        if (path == "external_id" and ref == "node-external")
        or (path == "terminal_context" and ref == FOREIGN_ID)
        else None
    )
    if path == "web_chat":
        manager.find_by_external_id.return_value = foreign
    compact = CompactIdentityResolution(session=foreign) if path == "compact" else None
    event = make_event(
        HookEventType.SESSION_START, session_id="node-external", data=data, machine_id=NODE
    )

    with (
        patch(f"{_HANDOFF}.resolve_clear_continuation", return_value=_NO_CLEAR),
        patch(
            f"{_HANDOFF}.resolve_compact_continuation",
            return_value=compact or CompactIdentityResolution(),
        ),
        patch(f"{_FLOW}.reconcile_compact_session_activity") as reconcile,
        patch.object(EventHandlers, "_handle_pre_created_session") as pre_created,
        pytest.raises(MachineOwnershipMismatchError),
    ):
        handlers.handle_session_start(event)

    pre_created.assert_not_called()
    reconcile.assert_not_called()
    manager.update.assert_not_called()
    manager.cache_session_mapping.assert_not_called()
    manager.backfill_terminal_context.assert_not_called()
    assert manager.register_session.call_args.kwargs["machine_id"] == NODE


def test_same_machine_precreated_row_binds(
    mock_dependencies: dict[str, Any],
    mock_empty_session_variable_manager: MagicMock,
) -> None:
    handlers, manager = _start_handlers(mock_dependencies)
    own = _row(NODE, id="node-precreated")
    manager.get.side_effect = lambda ref: own if ref == "node-precreated" else None
    event = make_event(
        HookEventType.SESSION_START,
        session_id="node-external",
        data={
            "source": "startup",
            "cwd": NODE_CWD,
            "terminal_context": {"gobby_session_id": "node-precreated"},
        },
        machine_id=NODE,
    )

    with patch.object(
        EventHandlers, "_handle_pre_created_session", return_value=HookResponse()
    ) as pre_created:
        handlers.handle_session_start(event)

    manager.update.assert_called_once_with("node-precreated", external_id="node-external")
    assert pre_created.call_args.kwargs["existing_session"] is own
    manager.register_session.assert_not_called()


def _lookup_service(manager: MagicMock, task_manager: MagicMock) -> SessionLookupService:
    return SessionLookupService(
        session_manager=manager,
        session_coordinator=MagicMock(),
        session_task_manager=task_manager,
        resolve_project_id=MagicMock(return_value="node-project"),
        logger=MagicMock(),
    )


@pytest.mark.parametrize("event_type", [HookEventType.SESSION_START, HookEventType.BEFORE_TOOL])
def test_session_header_skips_foreign_row(event_type: HookEventType) -> None:
    manager = MagicMock()
    manager.get.side_effect = lambda ref: _row(OTHER) if ref == FOREIGN_ID else None
    manager.get_session_id.return_value = None
    task_manager = MagicMock()
    service = _lookup_service(manager, task_manager)
    event = make_event(
        event_type,
        session_id="",
        data={"cwd": NODE_CWD},
        metadata={"_platform_session_id": FOREIGN_ID},
        machine_id=NODE,
    )
    event.project_id = "node-project"

    assert service.resolve(event) is None

    assert "_platform_session_id" not in event.metadata
    manager.backfill_terminal_context.assert_not_called()
    manager.revive_expired_terminal_session.assert_not_called()
    task_manager.get_worked_on_task.assert_not_called()


def test_non_start_lookup_skips_foreign_row() -> None:
    manager = MagicMock()
    manager.get.side_effect = (
        lambda ref: _row(OTHER, status="expired") if ref == FOREIGN_ID else None
    )
    manager.get_session_id.return_value = FOREIGN_ID
    task_manager = MagicMock()
    service = _lookup_service(manager, task_manager)
    event = make_event(
        HookEventType.BEFORE_TOOL, session_id="foreign-external", data={}, machine_id=NODE
    )
    event.project_id = "node-project"

    assert service.resolve(event) is None

    assert "_platform_session_id" not in event.metadata
    manager.backfill_terminal_context.assert_not_called()
    manager.revive_expired_terminal_session.assert_not_called()
    task_manager.get_worked_on_task.assert_not_called()


def test_non_start_compact_lookup_skips_foreign_row() -> None:
    manager = MagicMock()
    manager.get_session_id.return_value = None
    manager.lookup_session_id.return_value = None
    manager.recover_session.return_value = None
    manager.register_session.side_effect = _refuse_foreign_registration
    service = _lookup_service(manager, MagicMock())
    event = make_event(
        HookEventType.BEFORE_TOOL,
        session_id="node-external",
        data={"terminal_context": {"tmux_pane": "%1"}},
        machine_id=NODE,
    )
    event.project_id = "node-project"

    with (
        patch(
            "gobby.hooks.session_lookup.resolve_compact_continuation",
            return_value=CompactIdentityResolution(session=_row(OTHER)),
        ),
        patch("gobby.hooks.session_lookup.reconcile_compact_session_activity") as reconcile,
        pytest.raises(MachineOwnershipMismatchError),
    ):
        service.resolve(event)

    reconcile.assert_not_called()
    manager.cache_session_mapping.assert_not_called()
    assert event.session_id == "node-external"
    assert "_platform_session_id" not in event.metadata


def test_project_context_skips_foreign_rows() -> None:
    foreign = _row(OTHER)
    manager = MagicMock()
    manager.get.return_value = foreign
    manager.get_cached_session.return_value = foreign
    manager.find_active_by_external_id.return_value = foreign
    manager.recover_session.return_value = foreign
    resolve_project_id = MagicMock(return_value="node-project")
    event = make_event(
        HookEventType.BEFORE_TOOL,
        session_id="foreign-external",
        data={"cwd": NODE_CWD, "terminal_context": {"gobby_session_id": "foreign-terminal"}},
        metadata={"_platform_session_id": FOREIGN_ID},
        machine_id=NODE,
    )

    resolution = resolve_hook_project_context(
        event, session_manager=manager, resolve_project_id=resolve_project_id
    )

    assert (resolution.project_id, resolution.source, resolution.session) == (
        "node-project",
        "cwd",
        None,
    )
    assert event.project_id == "node-project"
    resolve_project_id.assert_called_once_with(None, NODE_CWD)


def test_remote_terminal_context_skips_local_process_inspection() -> None:
    reported = {
        "tmux_pane": "%3",
        "parent_pid": os.getpid(),
        "parent_create_time": 1.0,
        "parent_name": "codex",
    }

    with patch("gobby.hooks.terminal_context.psutil.Process") as process:
        enriched = enrich_terminal_context_with_cwd(
            dict(reported), NODE_CWD, external_id="node-thread", machine_id=NODE
        )

    process.assert_not_called()
    assert enriched == {"tmux_pane": "%3", "parent_pid": os.getpid(), "cwd": NODE_CWD}
