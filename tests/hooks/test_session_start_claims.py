"""Clear-successor task-claim reassignment tests."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.hooks.event_handlers._session_start.claims import (
    filter_and_reassign_claimed_tasks,
    preserve_task_claim_state,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._transitions import close_task
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


class _SessionTaskLinker(Protocol):
    def link_task(self, session_id: str, task_id: str, action: str = "worked_on") -> None: ...


class _ClaimHandler:
    def __init__(
        self,
        task_manager: LocalTaskManager | None,
        session_task_manager: _SessionTaskLinker | None,
    ) -> None:
        self._task_manager = task_manager
        self._session_task_manager = session_task_manager
        self.logger = logging.getLogger("test.session-start-claims")


class _FailingLinkManager:
    def __init__(self, inner: SessionTaskManager, fail_ids: set[str]) -> None:
        self._inner = inner
        self._fail_ids = fail_ids
        self.calls: list[tuple[str, str, str]] = []

    def link_task(self, session_id: str, task_id: str, action: str = "worked_on") -> None:
        self.calls.append((session_id, task_id, action))
        if task_id in self._fail_ids:
            raise RuntimeError(f"link failed for {task_id}")
        self._inner.link_task(session_id, task_id, action)

    def get_session_tasks(self, session_id: str) -> list[dict[str, Any]]:
        return self._inner.get_session_tasks(session_id)


@dataclass
class _ClaimHarness:
    db: HubDatabase
    project_id: str
    predecessor_id: str
    successor_id: str
    third_id: str
    task_manager: LocalTaskManager
    session_task_manager: SessionTaskManager
    sv_mgr: SessionVariableManager
    sessions: SessionManager

    def handler(
        self,
        *,
        session_task_manager: _SessionTaskLinker | None = None,
    ) -> _ClaimHandler:
        links = self.session_task_manager if session_task_manager is None else session_task_manager
        return _ClaimHandler(self.task_manager, links)

    def register_session(self, label: str) -> str:
        row = self.sessions.register(
            external_id=f"{label}-{uuid4()}",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=self.project_id,
        )
        return row.id

    def create_claimed_task(self, title: str, *, owner_id: str | None = None) -> Task:
        task = self.task_manager.create_task(
            self.project_id,
            title=title,
            validation_criteria="Test task completion is observable.",
        )
        owner = owner_id or self.predecessor_id
        self.task_manager.claim_task(task.id, session_id=owner)
        self.session_task_manager.link_task(owner, task.id, "claimed")
        return task


def _make_harness(db: HubDatabase) -> _ClaimHarness:
    project = LocalProjectManager(db).create(
        name=f"clear-claims-{uuid4().hex[:8]}",
        repo_path="/tmp/clear-claims",
    )
    sessions = SessionManager(db)
    predecessor = sessions.register(
        external_id=f"pred-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project.id,
    )
    successor = sessions.register(
        external_id=f"succ-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project.id,
    )
    third = sessions.register(
        external_id=f"third-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project.id,
    )
    return _ClaimHarness(
        db=db,
        project_id=project.id,
        predecessor_id=predecessor.id,
        successor_id=successor.id,
        third_id=third.id,
        task_manager=LocalTaskManager(db),
        session_task_manager=SessionTaskManager(db),
        sv_mgr=SessionVariableManager(db),
        sessions=sessions,
    )


def _predecessor_vars(*tasks: Task) -> dict[str, Any]:
    claimed = {task.id: f"#{task.seq_num}" for task in tasks}
    return {
        "task_claimed": True,
        "claimed_tasks": claimed,
        "session_had_task": True,
    }


def _claimed_task_ids(manager: SessionTaskManager, session_id: str) -> set[str]:
    return {
        str(row["task"].id)
        for row in manager.get_session_tasks(session_id)
        if row["action"] == "claimed"
    }


def test_preserve_transfers_predecessor_claims_and_claimed_link(hub_db: HubDatabase) -> None:
    harness = _make_harness(hub_db)
    task = harness.create_claimed_task("Leaf owned by predecessor")

    preserve_task_claim_state(
        harness.handler(),
        harness.sv_mgr,
        harness.successor_id,
        harness.predecessor_id,
        _predecessor_vars(task),
    )

    transferred = harness.task_manager.get_task(task.id)
    assert transferred.claimed_by_session_id == harness.successor_id
    assert task.id in _claimed_task_ids(harness.session_task_manager, harness.successor_id)
    successor_vars = harness.sv_mgr.get_variables(harness.successor_id)
    assert successor_vars.get("task_claimed") is True
    assert successor_vars.get("session_had_task") is True
    assert successor_vars.get("claimed_tasks") == {task.id: f"#{task.seq_num}"}


def test_expected_owner_skips_claim_moved_to_third_session(hub_db: HubDatabase) -> None:
    harness = _make_harness(hub_db)
    kept = harness.create_claimed_task("Stays with predecessor")
    stolen = harness.create_claimed_task("Moved before successor bind")
    harness.task_manager.claim_task(stolen.id, session_id=harness.third_id, force=True)

    preserve_task_claim_state(
        harness.handler(),
        harness.sv_mgr,
        harness.successor_id,
        harness.predecessor_id,
        _predecessor_vars(kept, stolen),
    )

    assert harness.task_manager.get_task(kept.id).claimed_by_session_id == harness.successor_id
    assert harness.task_manager.get_task(stolen.id).claimed_by_session_id == harness.third_id
    successor_vars = harness.sv_mgr.get_variables(harness.successor_id)
    claimed = successor_vars.get("claimed_tasks") or {}
    assert claimed == {kept.id: f"#{kept.seq_num}"}
    assert stolen.id not in _claimed_task_ids(harness.session_task_manager, harness.successor_id)


def test_link_failure_compensates_ownership_and_keeps_committed_transfer(
    hub_db: HubDatabase,
) -> None:
    harness = _make_harness(hub_db)
    kept = harness.create_claimed_task("Link succeeds")
    broken = harness.create_claimed_task("Link explodes")
    failing_links = _FailingLinkManager(harness.session_task_manager, {broken.id})

    preserve_task_claim_state(
        harness.handler(session_task_manager=failing_links),
        harness.sv_mgr,
        harness.successor_id,
        harness.predecessor_id,
        _predecessor_vars(kept, broken),
    )

    assert harness.task_manager.get_task(kept.id).claimed_by_session_id == harness.successor_id
    assert harness.task_manager.get_task(broken.id).claimed_by_session_id == harness.predecessor_id
    assert kept.id in _claimed_task_ids(harness.session_task_manager, harness.successor_id)
    assert broken.id not in _claimed_task_ids(harness.session_task_manager, harness.successor_id)
    successor_vars = harness.sv_mgr.get_variables(harness.successor_id)
    assert successor_vars.get("claimed_tasks") == {kept.id: f"#{kept.seq_num}"}
    assert any(
        session_id == harness.successor_id and task_id == broken.id and action == "claimed"
        for session_id, task_id, action in failing_links.calls
    )


def test_per_task_errors_do_not_abort_remaining_transfers(hub_db: HubDatabase) -> None:
    harness = _make_harness(hub_db)
    kept = harness.create_claimed_task("Survives sibling lookup failure")
    missing_id = str(uuid4())
    predecessor_vars = _predecessor_vars(kept)
    predecessor_vars["claimed_tasks"][missing_id] = "#0"

    preserve_task_claim_state(
        harness.handler(),
        harness.sv_mgr,
        harness.successor_id,
        harness.predecessor_id,
        predecessor_vars,
    )

    assert harness.task_manager.get_task(kept.id).claimed_by_session_id == harness.successor_id
    successor_vars = harness.sv_mgr.get_variables(harness.successor_id)
    assert successor_vars.get("claimed_tasks") == {kept.id: f"#{kept.seq_num}"}
    assert missing_id not in (successor_vars.get("claimed_tasks") or {})


def test_closed_task_is_skipped_without_aborting(hub_db: HubDatabase) -> None:
    harness = _make_harness(hub_db)
    kept = harness.create_claimed_task("Still open")
    closed = harness.create_claimed_task("Already closed")
    close_task(hub_db, closed.id, reason="test")

    preserve_task_claim_state(
        harness.handler(),
        harness.sv_mgr,
        harness.successor_id,
        harness.predecessor_id,
        _predecessor_vars(kept, closed),
    )

    assert harness.task_manager.get_task(kept.id).claimed_by_session_id == harness.successor_id
    assert harness.task_manager.get_task(closed.id).claimed_by_session_id != harness.successor_id
    assert closed.id not in _claimed_task_ids(harness.session_task_manager, harness.successor_id)
    successor_vars = harness.sv_mgr.get_variables(harness.successor_id)
    assert successor_vars.get("claimed_tasks") == {kept.id: f"#{kept.seq_num}"}


def test_filter_and_reassign_returns_only_committed_transfers(hub_db: HubDatabase) -> None:
    harness = _make_harness(hub_db)
    kept = harness.create_claimed_task("Committed")
    stolen = harness.create_claimed_task("Stolen")
    harness.task_manager.claim_task(stolen.id, session_id=harness.third_id, force=True)
    claimed = {
        kept.id: f"#{kept.seq_num}",
        stolen.id: f"#{stolen.seq_num}",
        str(uuid4()): "#missing",
    }

    committed = filter_and_reassign_claimed_tasks(
        harness.handler(),
        harness.successor_id,
        harness.predecessor_id,
        claimed,
    )

    assert committed == {kept.id: f"#{kept.seq_num}"}
    assert harness.task_manager.get_task(kept.id).claimed_by_session_id == harness.successor_id
    assert harness.task_manager.get_task(stolen.id).claimed_by_session_id == harness.third_id


def test_concurrent_successors_transfer_exactly_once(hub_db: HubDatabase) -> None:
    harness = _make_harness(hub_db)
    task = harness.create_claimed_task("One successor may inherit")
    second_id = harness.register_session("succ-b")
    vars_payload = _predecessor_vars(task)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run(successor_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            preserve_task_claim_state(
                harness.handler(),
                harness.sv_mgr,
                successor_id,
                harness.predecessor_id,
                vars_payload,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=(harness.successor_id,)),
        threading.Thread(target=_run, args=(second_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    owner = harness.task_manager.get_task(task.id).claimed_by_session_id
    assert owner in {harness.successor_id, second_id}
    loser = second_id if owner == harness.successor_id else harness.successor_id
    assert task.id in _claimed_task_ids(harness.session_task_manager, owner)
    assert task.id not in _claimed_task_ids(harness.session_task_manager, loser)
    winner_vars = harness.sv_mgr.get_variables(owner)
    loser_vars = harness.sv_mgr.get_variables(loser)
    assert (winner_vars.get("claimed_tasks") or {}).get(task.id) == f"#{task.seq_num}"
    assert task.id not in (loser_vars.get("claimed_tasks") or {})
