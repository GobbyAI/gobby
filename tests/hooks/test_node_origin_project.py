"""Node-origin hook events take their project from the node's checkout rows (#24524)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.hooks.checkout_reads import delivery_scope
from gobby.hooks.events import HookEventType
from gobby.hooks.project_context import resolve_hook_project_context
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.workflows.hooks import WorkflowHookHandler
from tests.fixtures.postgres import TEST_USER_ID

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


@pytest.fixture
def node_machine(temp_db: HubDatabase) -> str:
    machine = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine, TEST_USER_ID)
    return machine


def _checkout(temp_db: HubDatabase, project_manager: Any, machine: str, root: Path) -> str:
    project = project_manager.create(name=f"node-{uuid4().hex[:8]}")
    LocalProjectCheckoutManager(temp_db).register(machine, project.id, str(root))
    return str(project.id)


def _session_manager(temp_db: HubDatabase) -> MagicMock:
    manager = MagicMock()
    manager.db = temp_db
    manager.get.return_value = None
    manager.get_cached_session.return_value = None
    manager.find_active_by_external_id.return_value = None
    manager.recover_session.return_value = None
    return manager


@pytest.mark.asyncio
async def test_node_cwd_resolves_from_node_rows(
    temp_db: HubDatabase,
    project_manager: Any,
    node_machine: str,
    tmp_path: Path,
) -> None:
    node_root = tmp_path / "node" / "repo"
    outer = _checkout(temp_db, project_manager, node_machine, node_root)
    inner = _checkout(temp_db, project_manager, node_machine, node_root / "nested")
    cwd = str(node_root / "nested" / "src")
    hub_walk = MagicMock(return_value="hub-marker-project")
    event = make_event(HookEventType.BEFORE_TOOL, data={"cwd": cwd}, machine_id=node_machine)

    with delivery_scope(node_machine, asyncio.get_running_loop(), cwd):
        resolution = resolve_hook_project_context(
            event, session_manager=_session_manager(temp_db), resolve_project_id=hub_walk
        )

    assert (resolution.project_id, resolution.source) == (inner, "cwd")
    assert event.project_id == inner
    hub_walk.assert_not_called()
    rows = LocalProjectCheckoutManager(temp_db).list_for_machine(node_machine)
    assert sorted(row.project_id for row in rows) == sorted([outer, inner])


@pytest.mark.asyncio
async def test_node_cwd_without_row_fails_closed(
    temp_db: HubDatabase,
    project_manager: Any,
    node_machine: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _checkout(temp_db, project_manager, node_machine, tmp_path / "node" / "other")
    cwd = str(tmp_path / "node" / "repo" / "src")
    hub_walk = MagicMock(return_value="hub-marker-project")
    manager = _session_manager(temp_db)
    loop = asyncio.get_running_loop()
    event = make_event(HookEventType.BEFORE_TOOL, data={"cwd": cwd}, machine_id=node_machine)

    with delivery_scope(node_machine, loop, cwd), pytest.raises(ValueError) as raised:
        resolve_hook_project_context(event, session_manager=manager, resolve_project_id=hub_walk)

    assert f"checkout_read_unavailable: no_checkout_row (machine {node_machine})" in str(
        raised.value
    )
    hub_walk.assert_not_called()

    daemon_context = MagicMock(side_effect=AssertionError("daemon current-context project read"))
    monkeypatch.setattr("gobby.utils.project_context.get_project_context", daemon_context)
    root_event = make_event(HookEventType.BEFORE_TOOL, data={"cwd": "/"}, machine_id=node_machine)

    with delivery_scope(node_machine, loop, "/"):
        resolution = resolve_hook_project_context(
            root_event, session_manager=manager, resolve_project_id=hub_walk
        )

    assert resolution.skipped is True
    assert resolution.project_id is None
    daemon_context.assert_not_called()
    hub_walk.assert_not_called()


@pytest.mark.asyncio
async def test_node_project_path_uses_node_row(
    temp_db: HubDatabase,
    project_manager: Any,
    node_machine: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node" / "repo"
    project_id = _checkout(temp_db, project_manager, node_machine, root)
    unregistered_id = str(project_manager.create(name=f"node-{uuid4().hex[:8]}").id)
    handler = WorkflowHookHandler()
    engine = MagicMock()
    engine.db = temp_db
    handler.rule_engine = engine
    linked = str(root / "linked-worktree")
    cases = [
        (project_id, None, str(root)),
        (project_id, str(root), str(root)),
        (project_id, linked, linked),
        (unregistered_id, linked, linked),
    ]
    events = []
    for project, _worktree_root, _expected in cases:
        event = make_event(
            HookEventType.BEFORE_TOOL, data={"cwd": str(root)}, machine_id=node_machine
        )
        event.project_id = project
        events.append(event)

    def hub_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("node event resolved a project path on the hub")

    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_local_machine_id", hub_forbidden
    )
    monkeypatch.setattr("gobby.storage.project_checkouts.require_root", hub_forbidden)
    monkeypatch.setattr("gobby.storage.project_checkouts.resolve_operation_root", hub_forbidden)
    monkeypatch.setattr(Path, "resolve", hub_forbidden)
    loop = asyncio.get_running_loop()

    for event, (_project, worktree_root, expected) in zip(events, cases, strict=True):
        with delivery_scope(node_machine, loop, str(root)):
            path = handler._resolve_project_path(event, worktree_root)
        assert path == expected
        assert event.metadata["project_path"] == expected
