"""Node checkout attribution and machine-scoped edit ownership (#24529)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.code_index.trigger import CodeIndexTrigger
from gobby.hooks import checkout_reads
from gobby.hooks.checkout_reads import delivery_scope
from gobby.hooks.event_handlers import EventHandlers
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.commit_guard import _live_path_owners
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import resolve_edit_pair
from tests.fixtures.postgres import TEST_USER_ID
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = pytest.mark.unit


class RecordingCodeIndex(CodeIndexTrigger):
    """Capture delivered notifications without starting an index worker."""

    def __init__(self) -> None:
        self.changes: list[tuple[str, str, str]] = []

    def notify_file_changed(self, file_path: str, project_id: str, root_path: str) -> None:
        self.changes.append((file_path, project_id, root_path))


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["inside", "outside", "no-root", "other-project"])
async def test_node_edit_pair_uses_node_facts(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = str(tmp_path / "shared-checkout")
    cwd = f"{root}/src"
    path = "link.py"
    node.add_file(NODE_MACHINE, path, b"node content")
    node.set_checkout(NODE_MACHINE, cwd, root=root, project_id="project-a")
    node.files[NODE_MACHINE][path].update(
        resolved=f"{root}/actual.py",
        project_root=None if case == "no-root" else root,
        project_id="project-b" if case == "other-project" else "project-a",
        kind="outside" if case == "outside" else "file",
    )
    hub_marker = MagicMock(side_effect=AssertionError("node edit read a hub project marker"))
    monkeypatch.setattr("gobby.utils.project_context.find_project_root", hub_marker)
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        pair = await asyncio.to_thread(
            resolve_edit_pair, path, cwd, project_id="project-a", db=None, variables={}
        )
    assert pair == ((root, "actual.py") if case == "inside" else None)
    hub_marker.assert_not_called()
    assert node.requests[0][2]["query"]["paths"] == [path]
    assert node.requests[0][2]["cwd"] == cwd


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path,relative",
    [
        ("./nested/../file.py", "src/file.py"),
        ("../../outside.py", None),
        ("/another-checkout/file.py", None),
    ],
)
async def test_unavailable_node_edit_pair_is_lexical(
    node: FakeNode,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_path: str,
    relative: str | None,
) -> None:
    machine = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine, TEST_USER_ID)
    root = tmp_path / "node-checkout"
    cwd = str(root / "src")
    project_id = sample_project["id"]
    LocalProjectCheckoutManager(temp_db).register(machine, project_id, str(root))
    node.fail(machine, "channel_closed")
    hub_marker = MagicMock(side_effect=AssertionError("unavailable node read a hub marker"))
    monkeypatch.setattr("gobby.utils.project_context.find_project_root", hub_marker)
    with delivery_scope(machine, asyncio.get_running_loop(), cwd):
        pair = await asyncio.to_thread(
            resolve_edit_pair, file_path, cwd, project_id=project_id, db=temp_db, variables={}
        )
    assert pair == ((str(root), relative) if relative is not None else None)
    hub_marker.assert_not_called()
    assert len(node.requests) == 1


@pytest.mark.asyncio
async def test_node_edit_skips_hub_code_index(
    node: FakeNode,
    event_handlers: EventHandlers,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger = RecordingCodeIndex()
    event_handlers._code_index_trigger = trigger
    resolver = MagicMock(return_value="project-a")
    monkeypatch.setattr("gobby.hooks.project_context.ProjectIdResolver.resolve_for_index", resolver)
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        event_handlers._notify_code_index(tmp_path, "changed.py")
    assert trigger.changes == []
    resolver.assert_not_called()
    # The same handler still indexes local events after the node delivery ends.
    event_handlers._notify_code_index(tmp_path, "changed.py")
    assert trigger.changes == [("changed.py", "project-a", str(tmp_path))]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_path_owners_are_machine_scoped(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = get_machine_id()
    assert hub is not None
    machine = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine, TEST_USER_ID)
    monkeypatch.setattr(checkout_reads, "get_machine_id", lambda: hub)
    project_id = sample_project["id"]
    owners = []
    for origin in (hub, machine):
        # Register each session as its own daemon would; ownership reads run on the hub.
        with monkeypatch.context() as registration:
            registration.setattr(
                "gobby.storage.workspace_machine_scope.require_machine_id",
                lambda machine_id=origin: machine_id,
            )
            session = session_manager.register(
                external_id=f"edit-owner-{origin}",
                machine_id=origin,
                source="codex",
                project_id=project_id,
            )
        SessionVariableManager(temp_db).merge_variables(
            session.id,
            {
                "session_dirty_files": ["shared.py"],
                "session_dirty_file_checkouts": {str(tmp_path): ["shared.py"]},
            },
        )
        owners.append(session.id)
    hub_owners = _live_path_owners(temp_db, checkout_root=str(tmp_path))
    assert [owner.owner_session_id for owner in hub_owners["shared.py"]] == [owners[0]]
    with delivery_scope(machine, asyncio.get_running_loop(), str(tmp_path)):
        node_owners = _live_path_owners(temp_db, checkout_root=str(tmp_path))
        assert [owner.owner_session_id for owner in node_owners["shared.py"]] == [owners[1]]
        assert (
            _live_path_owners(temp_db, checkout_root=str(tmp_path), exclude_session_id=owners[1])
            == {}
        )
