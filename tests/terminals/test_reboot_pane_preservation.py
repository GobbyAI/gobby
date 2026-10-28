"""Reboot must preserve recoverable pane identity for both launch origins (#24763)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.storage.workspaces import WorkspaceManager
from gobby.terminals.host_reconcile import reconcile_host_inventory
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX
from tests.terminals.host_fakes import FakeListRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


@dataclass(frozen=True)
class RecoveryPane:
    workspace_id: str
    tab_id: str
    pane_id: str
    terminal_id: str
    session_id: str
    task_id: str
    cwd: str
    external_id: str
    host_terminal_id: str


@pytest.fixture(params=["operator", "managed"])
def recovery_pane(
    request: pytest.FixtureRequest,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> RecoveryPane:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    sessions = SessionManager(temp_db)
    project_id = str(sample_project["id"])
    cwd = str(tmp_path)
    parent = sessions.register(
        external_id="reboot-parent", machine_id=MACHINE_ID, source="codex", project_id=project_id
    )
    session = sessions.register(
        external_id="original-provider-conversation",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
        parent_session_id=parent.id if request.param == "managed" else None,
        workspace_path=cwd,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=project_id,
        title="Keep this pane's existing claim",
        validation_criteria="Recovery preserves pane and claim identity.",
    )
    tasks.claim_task(task.id, session_id=session.id)
    run_id = None
    if request.param == "managed":
        runs = LocalAgentRunManager(temp_db)
        run = runs.create(
            parent_session_id=parent.id, provider="codex", prompt="continue", task_id=task.id
        )
        runs.update_child_session(run.id, session.id)
        run_id = run.id
    terminals = TerminalManager(temp_db)
    terminal_id, host_terminal_id = str(uuid4()), str(uuid4())
    terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=MACHINE_ID,
        session_id=session.id,
        agent_run_id=run_id,
    )
    terminals.promote_to_live(
        terminal_id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key("before-reboot", host_terminal_id),
        host_epoch="before-reboot",
    )
    workspaces = WorkspaceManager(temp_db)
    workspace, _created = workspaces.create(MACHINE_ID, "Recovered work")
    created = workspaces.create_tab(
        workspace.id, pane_id=str(uuid4()), project_id=project_id, title="Original tab"
    )
    tab, pane = created.tabs[0], created.panes[0]
    workspaces.set_pane_terminal(pane.id, terminal_id, owns_terminal=True)
    workspaces.set_focus_hints(workspace.id, project_id=project_id, tab_id=tab.id, pane_id=pane.id)
    return RecoveryPane(
        workspace.id,
        tab.id,
        pane.id,
        terminal_id,
        session.id,
        task.id,
        cwd,
        session.external_id,
        host_terminal_id,
    )


async def test_reboot_inventory_and_workspace_sweep_keep_recoverable_pane(
    temp_db: HubDatabase, recovery_pane: RecoveryPane
) -> None:
    pane = recovery_pane
    workspaces = WorkspaceManager(temp_db)
    terminals = TerminalManager(temp_db)
    task_before = LocalTaskManager(temp_db).get_task(pane.task_id)
    tab_before = workspaces.list_tabs(pane.workspace_id)[0]
    kill = AsyncMock()
    # A new host epoch has no old processes. Repeated startup must retain the
    # pane even after reconciliation establishes that the old process is gone.
    for _ in range(2):
        await reconcile_host_inventory(
            terminal_manager=terminals,
            machine_id=MACHINE_ID,
            host_epoch="after-reboot",
            host_rows=[],
            spawn_in_doubt_seconds=0,
            run_manager=None,
            kill=kill,
        )
        workspaces.sweep_dead_panes(pane.workspace_id)
    assert [row.id for row in workspaces.list_panes(pane.workspace_id)] == [pane.pane_id]
    assert workspaces.list_tabs(pane.workspace_id) == [tab_before]
    session = SessionManager(temp_db).get(pane.session_id)
    assert session is not None
    assert (session.external_id, session.workspace_path) == (pane.external_id, pane.cwd)
    assert LocalTaskManager(temp_db).get_task(pane.task_id) == task_before
    kill.assert_not_awaited()


async def test_surviving_terminal_is_adopted_without_replacing_pane_or_claim(
    temp_db: HubDatabase, recovery_pane: RecoveryPane
) -> None:
    pane = recovery_pane
    terminals = TerminalManager(temp_db)
    workspaces = WorkspaceManager(temp_db)
    terminal_before = terminals.get(pane.terminal_id)
    task_before = LocalTaskManager(temp_db).get_task(pane.task_id)
    kill = AsyncMock()
    for _ in range(2):
        await reconcile_host_inventory(
            terminal_manager=terminals,
            machine_id=MACHINE_ID,
            host_epoch="before-reboot",
            host_rows=[
                FakeListRow(
                    pane.terminal_id, pane.terminal_id, host_terminal_id=pane.host_terminal_id
                )
            ],
            spawn_in_doubt_seconds=0,
            run_manager=None,
            kill=kill,
        )
        workspaces.sweep_dead_panes(pane.workspace_id)
    assert terminals.get(pane.terminal_id) == terminal_before
    assert [row.id for row in workspaces.list_panes(pane.workspace_id)] == [pane.pane_id]
    assert LocalTaskManager(temp_db).get_task(pane.task_id) == task_before
    kill.assert_not_awaited()


async def test_explicitly_closed_pane_is_not_recreated_on_reboot(
    temp_db: HubDatabase, recovery_pane: RecoveryPane
) -> None:
    pane = recovery_pane
    workspaces = WorkspaceManager(temp_db)
    workspaces.remove_pane(pane.pane_id)
    for _ in range(2):
        await reconcile_host_inventory(
            terminal_manager=TerminalManager(temp_db),
            machine_id=MACHINE_ID,
            host_epoch="after-reboot",
            host_rows=[],
            spawn_in_doubt_seconds=0,
            run_manager=None,
            kill=AsyncMock(),
        )
        workspaces.sweep_dead_panes(pane.workspace_id)
    assert workspaces.list_panes(pane.workspace_id) == []
    assert workspaces.list_tabs(pane.workspace_id) == []
