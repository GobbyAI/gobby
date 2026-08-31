"""Tests for gobby.storage.checkpoints module."""

from __future__ import annotations

import pytest

from gobby.storage.checkpoints import Checkpoint, LocalCheckpointManager
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "22222222-2222-2222-2222-222222222222"
TASK_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
TASK_2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
CKPT_IDS = [f"cccccccc-cccc-4ccc-8ccc-ccccccccccc{i}" for i in range(5)]
RUN_IDS = [f"dddddddd-dddd-4ddd-8ddd-ddddddddddd{i}" for i in range(5)]
CKPT_1 = CKPT_IDS[1]
CKPT_2 = CKPT_IDS[2]
UNKNOWN_ID = "99999999-9999-9999-9999-999999999999"


@pytest.fixture(autouse=True)
def _seed_parents(temp_db: HubDatabase) -> None:
    """Insert parent records so checkpoint FK constraints pass."""
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, 'test')",
            (PROJECT_ID,),
        )
        conn.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
            "VALUES (%s, 'ext-1', '21000000-0000-4000-8000-000000000001', 'test', %s)",
            (SESSION_ID, PROJECT_ID),
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, task_type, category, validation_criteria, created_at, updated_at) "
            "VALUES (%s, %s, 'test task', 'task', 'code', "
            "'Storage fixture task; behavior asserted by the test.', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (TASK_1, PROJECT_ID),
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, task_type, category, validation_criteria, created_at, updated_at) "
            "VALUES (%s, %s, 'test task 2', 'task', 'code', "
            "'Storage fixture task; behavior asserted by the test.', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (TASK_2, PROJECT_ID),
        )
        for i in range(5):
            conn.execute(
                "INSERT INTO agent_runs (id, parent_session_id, machine_id, status, provider, prompt) "
                "VALUES (%s, %s, '21000000-0000-4000-8000-000000000001', 'running', 'test', 'test prompt')",
                (RUN_IDS[i], SESSION_ID),
            )


def _make_checkpoint(
    checkpoint_id: str = CKPT_1,
    task_id: str = TASK_1,
    session_id: str = SESSION_ID,
    run_id: str = RUN_IDS[1],
    seq: int = 1,
) -> Checkpoint:
    return Checkpoint(
        id=checkpoint_id,
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        ref_name=f"refs/gobby/ckpt/{task_id}/{seq}",
        commit_sha="abc123def456",
        parent_sha="000111222333",
        files_changed=3,
        message=f"auto-checkpoint for task {task_id}",
        created_at="2026-04-03 10:00:00",
    )


@pytest.fixture
def manager(temp_db: HubDatabase) -> LocalCheckpointManager:
    return LocalCheckpointManager(temp_db)


class TestCreate:
    def test_creates_and_returns(self, manager: LocalCheckpointManager) -> None:
        ckpt = _make_checkpoint()
        result = manager.create(ckpt)
        assert result.id == ckpt.id
        assert result.commit_sha == "abc123def456"

    def test_persists_to_db(self, manager: LocalCheckpointManager) -> None:
        ckpt = _make_checkpoint()
        manager.create(ckpt)
        fetched = manager.get(ckpt.id)
        assert fetched is not None
        assert fetched.task_id == TASK_1
        assert fetched.files_changed == 3


class TestGet:
    def test_returns_none_for_missing(self, manager: LocalCheckpointManager) -> None:
        assert manager.get(UNKNOWN_ID) is None

    def test_returns_checkpoint(self, manager: LocalCheckpointManager) -> None:
        ckpt = _make_checkpoint()
        manager.create(ckpt)
        result = manager.get(ckpt.id)
        assert result is not None
        assert result.ref_name == f"refs/gobby/ckpt/{TASK_1}/1"


class TestListForTask:
    def test_empty_for_unknown_task(self, manager: LocalCheckpointManager) -> None:
        assert manager.list_for_task(UNKNOWN_ID) == []

    def test_returns_checkpoints_newest_first(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint(checkpoint_id=CKPT_1, seq=1))
        ckpt2 = Checkpoint(
            id=CKPT_2,
            task_id=TASK_1,
            session_id=SESSION_ID,
            run_id=RUN_IDS[2],
            ref_name=f"refs/gobby/ckpt/{TASK_1}/2",
            commit_sha="def456",
            parent_sha="abc123",
            files_changed=1,
            message="checkpoint 2",
            created_at="2026-04-03 11:00:00",
        )
        manager.create(ckpt2)
        results = manager.list_for_task(TASK_1)
        assert len(results) == 2
        assert results[0].id == CKPT_2  # Newest first

    def test_filters_by_task(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint(checkpoint_id=CKPT_1, task_id=TASK_1))
        manager.create(_make_checkpoint(checkpoint_id=CKPT_2, task_id=TASK_2))
        assert len(manager.list_for_task(TASK_1)) == 1


class TestDelete:
    def test_deletes_existing(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint())
        assert manager.delete(CKPT_1) is True
        assert manager.get(CKPT_1) is None

    def test_returns_false_for_missing(self, manager: LocalCheckpointManager) -> None:
        assert manager.delete(UNKNOWN_ID) is False


class TestDeleteOld:
    def test_keeps_n_latest(self, manager: LocalCheckpointManager) -> None:
        for i in range(5):
            ckpt = Checkpoint(
                id=CKPT_IDS[i],
                task_id=TASK_1,
                session_id=SESSION_ID,
                run_id=RUN_IDS[i],
                ref_name=f"refs/gobby/ckpt/{TASK_1}/{i}",
                commit_sha=f"sha-{i}",
                parent_sha="parent",
                files_changed=1,
                message="checkpoint",
                created_at=f"2026-04-03 1{i}:00:00",
            )
            manager.create(ckpt)

        deleted = manager.delete_old(TASK_1, keep_latest=2)
        assert deleted == 3
        remaining = manager.list_for_task(TASK_1)
        assert len(remaining) == 2
        # Verify the two newest checkpoints are retained (ordered newest first)
        assert remaining[0].id == CKPT_IDS[4]
        assert remaining[1].id == CKPT_IDS[3]

    def test_noop_when_under_limit(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint())
        assert manager.delete_old(TASK_1, keep_latest=5) == 0


class TestCountForTask:
    def test_zero_for_unknown(self, manager: LocalCheckpointManager) -> None:
        assert manager.count_for_task(UNKNOWN_ID) == 0

    def test_counts_correctly(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint(checkpoint_id=CKPT_1))
        manager.create(_make_checkpoint(checkpoint_id=CKPT_2))
        assert manager.count_for_task(TASK_1) == 2


class TestToDict:
    def test_to_dict_returns_expected_fields(self) -> None:
        ckpt = _make_checkpoint()
        d = ckpt.to_dict()
        assert d["id"] == CKPT_1
        assert d["task_id"] == TASK_1
        assert d["ref_name"] == f"refs/gobby/ckpt/{TASK_1}/1"
        assert d["files_changed"] == 3
