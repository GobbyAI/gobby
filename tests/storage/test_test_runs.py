"""Tests for TestRunStorage CRUD operations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.test_runs import TestRunStorage

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[LocalDatabase]:
    """Create a temporary database with schema."""
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def storage(temp_db: LocalDatabase) -> TestRunStorage:
    return TestRunStorage(temp_db)


class TestCreateRun:
    def test_create_run_returns_test_run(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check src/")

        assert run.id.startswith("tr-")
        assert run.category == "lint"
        assert run.command == "ruff check src/"
        assert run.status == "running"
        assert run.started_at is not None

    def test_create_run_with_session(self, storage: TestRunStorage) -> None:
        run = storage.create_run(
            category="unit_tests",
            command="pytest tests/",
            session_id="sess-123",
            project_id="proj-456",
        )

        assert run.session_id == "sess-123"
        assert run.project_id == "proj-456"


class TestUpdateRun:
    def test_update_run_fields(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check")

        updated = storage.update_run(
            run.id,
            status="completed",
            exit_code=0,
            summary="All checks passed.",
        )

        assert updated is not None
        assert updated.status == "completed"
        assert updated.exit_code == 0
        assert updated.summary == "All checks passed."

    def test_update_run_invalid_field_raises(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check")

        with pytest.raises(ValueError, match="Invalid field names"):
            storage.update_run(run.id, bogus_field="value")

    def test_update_nonexistent_run(self, storage: TestRunStorage) -> None:
        result = storage.update_run("tr-nonexistent", status="completed")
        assert result is None


class TestGetRun:
    def test_get_run_by_id(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="type_check", command="mypy src/")

        fetched = storage.get_run(run.id)
        assert fetched is not None
        assert fetched.id == run.id
        assert fetched.category == "type_check"

    def test_get_nonexistent_run(self, storage: TestRunStorage) -> None:
        assert storage.get_run("tr-nonexistent") is None


class TestListRuns:
    def test_list_runs_empty(self, storage: TestRunStorage) -> None:
        runs = storage.list_runs()
        assert runs == []

    def test_list_runs_by_session(self, storage: TestRunStorage) -> None:
        storage.create_run(category="lint", command="ruff", session_id="s1")
        storage.create_run(category="lint", command="ruff", session_id="s2")

        runs = storage.list_runs(session_id="s1")
        assert len(runs) == 1
        assert runs[0].session_id == "s1"

    def test_list_runs_limit(self, storage: TestRunStorage) -> None:
        for i in range(5):
            storage.create_run(category="lint", command=f"cmd{i}")

        runs = storage.list_runs(limit=3)
        assert len(runs) == 3


class TestCleanup:
    def test_cleanup_old_runs(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check")

        # Manually backdate the run
        storage.db.execute(
            "UPDATE test_runs SET created_at = datetime('now', '-30 days') WHERE id = ?",
            (run.id,),
        )

        deleted = storage.cleanup_old_runs(days=7)
        assert deleted == 1
        assert storage.get_run(run.id) is None

    def test_cleanup_preserves_recent(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check")

        deleted = storage.cleanup_old_runs(days=7)
        assert deleted == 0
        assert storage.get_run(run.id) is not None


class TestTestRunModel:
    def test_to_dict(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check")
        d = run.to_dict()

        assert d["id"] == run.id
        assert d["category"] == "lint"
        assert d["command"] == "ruff check"
        assert "started_at" in d
        assert "created_at" in d

    def test_to_brief(self, storage: TestRunStorage) -> None:
        run = storage.create_run(category="lint", command="ruff check")
        brief = run.to_brief()

        assert brief["run_id"] == run.id
        assert brief["category"] == "lint"
        assert brief["status"] == "running"
        assert "command" not in brief
