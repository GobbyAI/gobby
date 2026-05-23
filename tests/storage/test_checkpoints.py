"""Tests for gobby.storage.checkpoints module."""

from __future__ import annotations

import pytest

from gobby.storage.checkpoints import Checkpoint, LocalCheckpointManager
from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _seed_parents(temp_db: LocalDatabase) -> None:
    """Insert parent records so checkpoint FK constraints pass."""
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, repo_path) VALUES ('proj-1', 'test', '/tmp/test')"
        )
        conn.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
            "VALUES ('sess-1', 'ext-1', 'machine-1', 'test', 'proj-1')"
        )
        conn.execute(
            "INSERT INTO tasks (id, project_id, title, task_type, category, created_at, updated_at) "
            "VALUES ('task-1', 'proj-1', 'test task', 'task', 'code', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO tasks (id, project_id, title, task_type, category, created_at, updated_at) "
            "VALUES ('task-2', 'proj-1', 'test task 2', 'task', 'code', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        for i in range(5):
            conn.execute(
                "INSERT INTO agent_runs (id, parent_session_id, status, provider, prompt) "
                "VALUES (?, 'sess-1', 'running', 'test', 'test prompt')",
                (f"run-{i}",),
            )


def _make_checkpoint(
    checkpoint_id: str = "ckpt-1",
    task_id: str = "task-1",
    session_id: str = "sess-1",
    run_id: str = "run-1",
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
def manager(temp_db: LocalDatabase) -> LocalCheckpointManager:
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
        assert fetched.task_id == "task-1"
        assert fetched.files_changed == 3


class TestGet:
    def test_returns_none_for_missing(self, manager: LocalCheckpointManager) -> None:
        assert manager.get("nonexistent") is None

    def test_returns_checkpoint(self, manager: LocalCheckpointManager) -> None:
        ckpt = _make_checkpoint()
        manager.create(ckpt)
        result = manager.get(ckpt.id)
        assert result is not None
        assert result.ref_name == "refs/gobby/ckpt/task-1/1"


class TestListForTask:
    def test_empty_for_unknown_task(self, manager: LocalCheckpointManager) -> None:
        assert manager.list_for_task("unknown") == []

    def test_returns_checkpoints_newest_first(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint(checkpoint_id="ckpt-1", seq=1))
        ckpt2 = Checkpoint(
            id="ckpt-2",
            task_id="task-1",
            session_id="sess-1",
            run_id="run-2",
            ref_name="refs/gobby/ckpt/task-1/2",
            commit_sha="def456",
            parent_sha="abc123",
            files_changed=1,
            message="checkpoint 2",
            created_at="2026-04-03 11:00:00",
        )
        manager.create(ckpt2)
        results = manager.list_for_task("task-1")
        assert len(results) == 2
        assert results[0].id == "ckpt-2"  # Newest first

    def test_filters_by_task(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint(checkpoint_id="ckpt-1", task_id="task-1"))
        manager.create(_make_checkpoint(checkpoint_id="ckpt-2", task_id="task-2"))
        assert len(manager.list_for_task("task-1")) == 1


class TestDelete:
    def test_deletes_existing(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint())
        assert manager.delete("ckpt-1") is True
        assert manager.get("ckpt-1") is None

    def test_returns_false_for_missing(self, manager: LocalCheckpointManager) -> None:
        assert manager.delete("nonexistent") is False


class TestDeleteOld:
    def test_keeps_n_latest(self, manager: LocalCheckpointManager) -> None:
        for i in range(5):
            ckpt = Checkpoint(
                id=f"ckpt-{i}",
                task_id="task-1",
                session_id="sess-1",
                run_id=f"run-{i}",
                ref_name=f"refs/gobby/ckpt/task-1/{i}",
                commit_sha=f"sha-{i}",
                parent_sha="parent",
                files_changed=1,
                message="checkpoint",
                created_at=f"2026-04-03 1{i}:00:00",
            )
            manager.create(ckpt)

        deleted = manager.delete_old("task-1", keep_latest=2)
        assert deleted == 3
        remaining = manager.list_for_task("task-1")
        assert len(remaining) == 2
        # Verify the two newest checkpoints are retained (ordered newest first)
        assert remaining[0].id == "ckpt-4"
        assert remaining[1].id == "ckpt-3"

    def test_noop_when_under_limit(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint())
        assert manager.delete_old("task-1", keep_latest=5) == 0


class TestCountForTask:
    def test_zero_for_unknown(self, manager: LocalCheckpointManager) -> None:
        assert manager.count_for_task("unknown") == 0

    def test_counts_correctly(self, manager: LocalCheckpointManager) -> None:
        manager.create(_make_checkpoint(checkpoint_id="ckpt-1"))
        manager.create(_make_checkpoint(checkpoint_id="ckpt-2"))
        assert manager.count_for_task("task-1") == 2


class TestToDict:
    def test_to_dict_returns_expected_fields(self) -> None:
        ckpt = _make_checkpoint()
        d = ckpt.to_dict()
        assert d["id"] == "ckpt-1"
        assert d["task_id"] == "task-1"
        assert d["ref_name"] == "refs/gobby/ckpt/task-1/1"
        assert d["files_changed"] == 3
