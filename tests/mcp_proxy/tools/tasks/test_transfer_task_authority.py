"""RED-first tests for transferring activation-receipt authority (#23157)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT
from gobby.tasks.close_receipts import (
    ACTIVATION,
    CloseReceipt,
    CloseReceiptError,
    list_close_receipts,
    record_close_receipt,
)
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

_LANDED = "fedfd114b6e31103849ccfc8c1382597835a2d11"
_MACHINE = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", _MACHINE):
        yield


class _Fixtures:
    def __init__(self, sessions: SessionManager, project_id: str) -> None:
        self.sessions = sessions
        self.project_id = project_id
        self.filer = self._add("Filer")
        self.claimant = self._add("Claimant")
        self.successor = self._add("Successor")

    def _add(self, title: str) -> Session:
        return self.sessions.register(
            external_id=f"authority-{title.lower()}-{uuid4()}",
            machine_id=_MACHINE,
            source="codex",
            project_id=self.project_id,
        )


@pytest.fixture
def authority_task(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> tuple[LocalTaskManager, Task, _Fixtures]:
    manager = LocalTaskManager(temp_db)
    fixtures = _Fixtures(SessionManager(temp_db), sample_project["id"])
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Authority transfer target",
        created_in_session_id=fixtures.filer.id,
        claimed_by_session_id=fixtures.claimant.id,
        category="code",
        validation_criteria="Receipt evidence reaches the close reviewer.",
    )
    temp_db.execute(
        "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s",
        (fixtures.filer.id, task.id),
    )
    return manager, manager.get_task(task.id), fixtures


def _transfer(
    manager: LocalTaskManager, task: Task, caller: Session, reason: str = "authority handoff"
) -> Task:
    return manager.transfer_task_authority(task.id, caller_session_id=caller.id, reason=reason)


def _activation(db: HubDatabase, task: Task, author: str) -> CloseReceipt:
    receipt, created = record_close_receipt(
        db, task=task, author_session_id=author, kind=ACTIVATION, commit_sha=_LANDED
    )
    assert created is True
    return receipt


def _seq_ref(db: HubDatabase, session_id: str) -> str:
    row = db.fetchone("SELECT seq_num FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    return f"#{row['seq_num']}"


def test_transfer_refused_while_creator_still_live(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    with pytest.raises(ValueError, match="still live"):
        _transfer(manager, task, fixtures.successor)


def test_transfer_refused_while_delegator_still_live(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (fixtures.filer.id,))
    live = fixtures._add("Live delegator")
    temp_db.execute(
        "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s", (live.id, task.id)
    )
    with pytest.raises(ValueError, match="still live"):
        _transfer(manager, task, fixtures.successor)


def test_transfer_refused_for_claimant(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (fixtures.filer.id,))
    temp_db.execute("UPDATE tasks SET delegated_by_session_id = NULL WHERE id = %s", (task.id,))
    with pytest.raises(ValueError, match="claimant"):
        _transfer(manager, task, fixtures.claimant)


def test_transfer_refused_for_task_close_reviewer(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    reviewer = fixtures._add("Close reviewer")
    temp_db.execute(
        """
        INSERT INTO agent_runs (
            id, parent_session_id, child_session_id, machine_id, status, provider,
            prompt, agent_name
        )
        VALUES (%s, %s, %s, %s, 'running', 'codex', 'review', %s)
        """,
        (
            str(uuid4()),
            fixtures.claimant.id,
            reviewer.id,
            require_machine_id(),
            TASK_CLOSE_REVIEWER_AGENT,
        ),
    )
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (fixtures.filer.id,))
    with pytest.raises(ValueError, match="reviewer"):
        _transfer(manager, task, reviewer)


def _expire(db: HubDatabase, *sessions: Session) -> None:
    for session in sessions:
        db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (session.id,))


def test_transfer_succeeds_after_both_authorities_expire(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    _expire(temp_db, fixtures.filer)

    updated = _transfer(manager, task, fixtures.successor, reason="filed by an expired seat")

    assert updated.delegated_by_session_id == fixtures.successor.id
    assert updated.delegated_to_session_id is None
    assert _activation(temp_db, updated, fixtures.successor.id).author_session_id == (
        fixtures.successor.id
    )
    bystander = fixtures._add("Bystander")
    with pytest.raises(CloseReceiptError, match="creator or delegator"):
        _activation(temp_db, updated, bystander.id)


def test_second_caller_refused_after_a_transfer(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    _expire(temp_db, fixtures.filer)
    _transfer(manager, task, fixtures.successor)
    other = fixtures._add("Second caller")

    with pytest.raises(ValueError, match="still live"):
        _transfer(manager, task, other)


def test_transfer_is_idempotent_for_the_same_caller(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    _expire(temp_db, fixtures.filer)
    first = _transfer(manager, task, fixtures.successor, reason="first reason")
    second = _transfer(manager, task, fixtures.successor, reason="ignored repeat")

    assert second.delegated_by_session_id == fixtures.successor.id
    assert second.delegation_reason == first.delegation_reason
    assert second.delegation_reason is not None
    assert "first reason" in second.delegation_reason
    assert "authority transferred" in second.delegation_reason


def test_transfer_preserves_receiver_and_prior_reason(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    receiver = fixtures._add("Prior receiver")
    temp_db.execute(
        "UPDATE tasks SET delegated_to_session_id = %s, delegation_reason = %s WHERE id = %s",
        (receiver.id, "original lane assignment", task.id),
    )
    _expire(temp_db, fixtures.filer)
    updated = _transfer(manager, task, fixtures.successor, reason="seat rotated")

    assert updated.delegated_to_session_id == receiver.id
    filer_ref = _seq_ref(temp_db, fixtures.filer.id)
    successor_ref = _seq_ref(temp_db, fixtures.successor.id)
    assert updated.delegation_reason == (
        "original lane assignment | authority transferred from "
        f"{filer_ref} (status=expired) to {successor_ref}: seat rotated"
    )


def test_tool_takes_caller_from_session_context(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    import asyncio

    manager, task, fixtures = authority_task
    _expire(temp_db, fixtures.filer)
    registry = create_task_registry(manager)

    with session_context_for_test(fixtures.successor.id):
        result = asyncio.run(
            registry.call("transfer_task_authority", {"task_id": task.id, "reason": "seat rotated"})
        )

    assert "error" not in result, result
    assert result["delegated_by_session_id"] == fixtures.successor.id
    with pytest.raises(CloseReceiptError, match="claimant"):
        _activation(temp_db, manager.get_task(task.id), fixtures.claimant.id)


def test_transfer_preserves_receipts_recorded_before_it(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    earlier = _activation(temp_db, task, fixtures.filer.id)
    _expire(temp_db, fixtures.filer)

    updated = _transfer(manager, task, fixtures.successor)
    later = _activation(temp_db, updated, fixtures.successor.id)

    assert [receipt.id for receipt in list_close_receipts(temp_db, task.id)] == [
        earlier.id,
        later.id,
    ]
    fetched = manager.get_task(task.id)
    assert fetched.delegated_by_session_id == fixtures.successor.id
    assert fetched.created_in_session_id == fixtures.filer.id


def _set_authority(db: HubDatabase, task: Task, creator: str | None, delegator: str | None) -> None:
    db.execute(
        "UPDATE tasks SET created_in_session_id = %s, delegated_by_session_id = %s WHERE id = %s",
        (creator, delegator, task.id),
    )


def test_transfer_from_an_expired_creator_with_no_delegator(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    _set_authority(temp_db, task, fixtures.filer.id, None)
    _expire(temp_db, fixtures.filer)

    updated = _transfer(manager, task, fixtures.successor, reason="filer expired")

    assert updated.delegated_by_session_id == fixtures.successor.id
    assert updated.created_in_session_id == fixtures.filer.id
    assert updated.delegation_reason == (
        f"authority transferred from {_seq_ref(temp_db, fixtures.filer.id)} (status=expired) "
        f"to {_seq_ref(temp_db, fixtures.successor.id)}: filer expired"
    )
    assert _activation(temp_db, updated, fixtures.successor.id).author_session_id == (
        fixtures.successor.id
    )


def test_transfer_names_the_delegator_when_it_differs_from_the_creator(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    delegator = fixtures._add("Delegator")
    _set_authority(temp_db, task, fixtures.filer.id, delegator.id)
    _expire(temp_db, fixtures.filer, delegator)

    updated = _transfer(manager, task, fixtures.successor, reason="delegator expired")

    assert updated.delegated_by_session_id == fixtures.successor.id
    assert updated.delegation_reason == (
        f"authority transferred from {_seq_ref(temp_db, delegator.id)} (status=expired) "
        f"to {_seq_ref(temp_db, fixtures.successor.id)}: delegator expired"
    )
    assert _activation(temp_db, updated, fixtures.successor.id).author_session_id == (
        fixtures.successor.id
    )


def test_transfer_note_falls_back_to_session_ids_without_seq_numbers(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    _set_authority(temp_db, task, fixtures.filer.id, None)
    _expire(temp_db, fixtures.filer)
    temp_db.execute(
        "UPDATE sessions SET seq_num = NULL WHERE id = ANY(%s)",
        ([fixtures.filer.id, fixtures.successor.id],),
    )

    updated = _transfer(manager, task, fixtures.successor, reason="unnumbered seats")

    assert updated.delegation_reason == (
        f"authority transferred from {fixtures.filer.id} (status=expired) "
        f"to {fixtures.successor.id}: unnumbered seats"
    )


def test_transfer_note_records_a_task_with_no_recorded_authority(
    temp_db: HubDatabase, authority_task: tuple[LocalTaskManager, Task, _Fixtures]
) -> None:
    manager, task, fixtures = authority_task
    _set_authority(temp_db, task, None, None)

    updated = _transfer(manager, task, fixtures.successor, reason="operator-filed task")

    assert updated.delegated_by_session_id == fixtures.successor.id
    assert updated.delegation_reason == (
        "authority transferred from no recorded session "
        f"to {_seq_ref(temp_db, fixtures.successor.id)}: operator-filed task"
    )
