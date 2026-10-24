"""PostgreSQL tests for daemon-recorded landing receipts."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_receipts import (
    CLOSE_RECEIPT_AUTHOR_TYPE,
    LANDING,
    CloseReceipt,
    CloseReceiptError,
    list_close_receipts,
    record_close_receipt,
)
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit

_TIP = "871d706" + "a" * 33
_LANDED = "fedfd114b6e31103849ccfc8c1382597835a2d11"


class _Sessions:
    def __init__(self, db: HubDatabase, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.next_seq = 900

    def add(self, title: str) -> str:
        session_id = str(uuid4())
        self.next_seq += 1
        self.db.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                status, agent_depth, seq_num
            )
            VALUES (%s, %s, %s, 'test', %s, %s, 'active', 0, %s)
            """,
            (
                session_id,
                f"ext-{session_id}",
                require_machine_id(),
                self.project_id,
                title,
                self.next_seq,
            ),
        )
        return session_id


@pytest.fixture
def sessions(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Sessions:
    return _Sessions(temp_db, sample_project["id"])


@pytest.fixture
def roles(sessions: _Sessions) -> dict[str, str]:
    return {
        "creator": sessions.add("Program Director"),
        "claimant": sessions.add("Lane seat"),
        "lander": sessions.add("Dev Manager"),
        "bystander": sessions.add("Other lane"),
    }


@pytest.fixture
def task(temp_db: HubDatabase, sample_project: dict[str, Any], roles: dict[str, str]) -> Task:
    return LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        "Receipted task",
        validation_criteria="The landing is recorded once.",
        created_in_session_id=roles["creator"],
        claimed_by_session_id=roles["claimant"],
    )


def _record(
    db: HubDatabase, task: Task, author: str, *, sha: str = _TIP, facts: object = None
) -> tuple[CloseReceipt, bool]:
    return record_close_receipt(
        db, task=task, author_session_id=author, commit_sha=sha, facts=facts
    )


def test_receipt_is_stored_once_as_a_reserved_comment(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    first, created = _record(temp_db, task, roles["lander"], sha=_TIP.upper())
    replay, replay_created = _record(temp_db, task, roles["lander"], facts={"note": "again"})

    assert created is True
    assert replay_created is False
    assert replay == first
    assert (first.kind, first.commit_sha) == (LANDING, _TIP)
    assert first.author_session_id == roles["lander"]
    row = temp_db.fetchone("SELECT author_type, body FROM task_comments WHERE id = %s", (first.id,))
    assert row is not None
    assert row["author_type"] == CLOSE_RECEIPT_AUTHOR_TYPE
    assert json.loads(row["body"]) == {"kind": LANDING, "commit_sha": _TIP, "facts": {}}
    assert [receipt.id for receipt in list_close_receipts(temp_db, task.id)] == [first.id]


def test_claimant_cannot_record_its_own_landing(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    with pytest.raises(CloseReceiptError, match="claimant"):
        _record(temp_db, task, roles["claimant"])
    assert list_close_receipts(temp_db, task.id) == []


def test_every_claim_writer_records_the_claimant(
    temp_db: HubDatabase, sample_project: dict[str, Any], roles: dict[str, str]
) -> None:
    manager = LocalTaskManager(temp_db)
    created = manager.create_task(
        sample_project["id"],
        "Claimed at creation",
        validation_criteria="Every claimant is recorded.",
        claimed_by_session_id=roles["claimant"],
    )
    manager.claim_task(created.id, roles["bystander"], expected_owner=roles["claimant"])
    manager.claim_task(created.id, roles["creator"], force=True)
    manager.escalate_task(created.id, "Released")
    rows = temp_db.fetchall(
        "SELECT session_id::text AS session_id FROM session_tasks"
        " WHERE task_id = %s AND action = 'claimed'",
        (created.id,),
    )
    assert {row["session_id"] for row in rows} == {
        roles["claimant"],
        roles["bystander"],
        roles["creator"],
    }


def test_landing_receipt_is_recorded_once_per_author_and_commit(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    facts = {"branch": "0.5.0", "landed_tip": _LANDED, "mode": "ff", "activation_class": "none"}
    first, created = _record(temp_db, task, roles["lander"], facts=facts)
    replay, replay_created = _record(temp_db, task, roles["lander"])
    another_author, author_created = _record(temp_db, task, roles["bystander"])
    another_commit, commit_created = _record(temp_db, task, roles["lander"], sha=_LANDED)
    assert created and author_created and commit_created
    assert replay_created is False
    assert replay == first
    assert first.facts == facts
    assert another_author.author_session_id == roles["bystander"]
    assert another_commit.commit_sha == _LANDED
    assert list_close_receipts(temp_db, task.id) == [first, another_author, another_commit]


def test_retired_receipt_kinds_read_as_nothing(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    for kind in ("activation", "landing_approval", "independent_review_approval"):
        temp_db.execute(
            """
            INSERT INTO task_comments (id, task_id, author, author_type, body)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                task.id,
                roles["creator"],
                CLOSE_RECEIPT_AUTHOR_TYPE,
                json.dumps({"kind": kind, "commit_sha": _TIP, "facts": {"reason": "restart"}}),
            ),
        )
    landing, created = _record(temp_db, task, roles["creator"])

    assert created is True
    assert list_close_receipts(temp_db, task.id) == [landing]


def test_receipt_and_authority_transfer_tools_are_retired(temp_db: HubDatabase) -> None:
    registry = create_task_registry(LocalTaskManager(temp_db))

    assert "link_commit" in registry
    assert "record_close_receipt" not in registry
    assert "transfer_task_authority" not in registry


@pytest.mark.parametrize(
    ("sha", "facts", "message"),
    [
        ("871d706", None, "full 40-character"),
        ("g" * 40, None, "full 40-character"),
        (_TIP, ["pid"], "facts must be an object"),
        (_TIP, {f"k{i}": i for i in range(17)}, "at most 16"),
        (_TIP, {"k" * 65: 1}, "fact key"),
        (_TIP, {"pid": 1.5}, "string, integer, or boolean"),
        (_TIP, {"log": {"nested": 1}}, "string, integer"),
        (_TIP, {"log": "x" * 301}, "exceeds 300"),
    ],
)
def test_malformed_receipts_are_refused(
    temp_db: HubDatabase,
    task: Task,
    roles: dict[str, str],
    sha: str,
    facts: object,
    message: str,
) -> None:
    with pytest.raises(CloseReceiptError, match=message):
        _record(temp_db, task, roles["lander"], sha=sha, facts=facts)
    assert list_close_receipts(temp_db, task.id) == []
