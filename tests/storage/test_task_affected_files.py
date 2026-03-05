"""Tests for TaskAffectedFileManager storage layer."""

import tempfile
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.task_affected_files import TaskAffectedFileManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db():
    """Create a fresh in-memory database with migrations applied."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        database = LocalDatabase(db_path)
        run_migrations(database)
        # Insert a dummy project and tasks for FK constraints
        database.execute(
            "INSERT INTO projects (id, name, repo_path) VALUES (?, ?, ?)",
            ("proj-1", "test-project", "/tmp/test"),
        )
        for tid in ("task-1", "task-2", "task-3"):
            database.execute(
                "INSERT INTO tasks (id, title, project_id, task_type, status, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (tid, f"Task {tid}", "proj-1", "task", "open", 2),
            )
        yield database
        database.close()


@pytest.fixture
def af_manager(db: LocalDatabase) -> TaskAffectedFileManager:
    return TaskAffectedFileManager(db)


class TestSetFiles:
    def test_set_files_creates_records(self, af_manager: TaskAffectedFileManager) -> None:
        results = af_manager.set_files("task-1", ["src/a.py", "src/b.py"])
        assert len(results) == 2
        assert {r.file_path for r in results} == {"src/a.py", "src/b.py"}
        assert all(r.annotation_source == "expansion" for r in results)

    def test_set_files_replaces_same_source(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/a.py", "src/b.py"], source="expansion")
        af_manager.set_files("task-1", ["src/c.py"], source="expansion")
        files = af_manager.get_files("task-1")
        assert [f.file_path for f in files] == ["src/c.py"]

    def test_set_files_preserves_other_sources(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/a.py"], source="expansion")
        af_manager.set_files("task-1", ["src/b.py"], source="manual")
        # Replace expansion files only
        af_manager.set_files("task-1", ["src/c.py"], source="expansion")
        files = af_manager.get_files("task-1")
        paths = {f.file_path for f in files}
        assert "src/b.py" in paths  # manual preserved
        assert "src/c.py" in paths  # new expansion
        assert "src/a.py" not in paths  # old expansion removed


class TestGetFiles:
    def test_get_files_empty(self, af_manager: TaskAffectedFileManager) -> None:
        files = af_manager.get_files("task-1")
        assert files == []

    def test_get_files_ordered_by_path(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/z.py", "src/a.py", "src/m.py"])
        files = af_manager.get_files("task-1")
        assert [f.file_path for f in files] == ["src/a.py", "src/m.py", "src/z.py"]


class TestAddFile:
    def test_add_file_success(self, af_manager: TaskAffectedFileManager) -> None:
        result = af_manager.add_file("task-1", "src/new.py")
        assert result is not None
        assert result.file_path == "src/new.py"
        assert result.annotation_source == "manual"

    def test_add_file_duplicate_returns_none(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.add_file("task-1", "src/a.py")
        result = af_manager.add_file("task-1", "src/a.py")
        assert result is None

    def test_add_file_custom_source(self, af_manager: TaskAffectedFileManager) -> None:
        result = af_manager.add_file("task-1", "src/obs.py", source="observed")
        assert result is not None
        assert result.annotation_source == "observed"


class TestRemoveFile:
    def test_remove_file_success(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.add_file("task-1", "src/a.py")
        assert af_manager.remove_file("task-1", "src/a.py") is True
        assert af_manager.get_files("task-1") == []

    def test_remove_file_not_found(self, af_manager: TaskAffectedFileManager) -> None:
        assert af_manager.remove_file("task-1", "nonexistent.py") is False


class TestFindOverlappingTasks:
    def test_no_overlap(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/a.py"])
        af_manager.set_files("task-2", ["src/b.py"])
        overlaps = af_manager.find_overlapping_tasks(["task-1", "task-2"])
        assert overlaps == {}

    def test_single_overlap(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/a.py", "src/shared.py"])
        af_manager.set_files("task-2", ["src/b.py", "src/shared.py"])
        overlaps = af_manager.find_overlapping_tasks(["task-1", "task-2"])
        assert len(overlaps) == 1
        pair = ("task-1", "task-2")
        assert pair in overlaps
        assert overlaps[pair] == ["src/shared.py"]

    def test_multiple_overlaps(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/a.py", "src/b.py"])
        af_manager.set_files("task-2", ["src/a.py", "src/b.py", "src/c.py"])
        overlaps = af_manager.find_overlapping_tasks(["task-1", "task-2"])
        pair = ("task-1", "task-2")
        assert set(overlaps[pair]) == {"src/a.py", "src/b.py"}

    def test_three_way_overlap(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/shared.py"])
        af_manager.set_files("task-2", ["src/shared.py"])
        af_manager.set_files("task-3", ["src/shared.py"])
        overlaps = af_manager.find_overlapping_tasks(["task-1", "task-2", "task-3"])
        assert len(overlaps) == 3  # 3 pairs: (1,2), (1,3), (2,3)

    def test_fewer_than_two_tasks(self, af_manager: TaskAffectedFileManager) -> None:
        assert af_manager.find_overlapping_tasks(["task-1"]) == {}
        assert af_manager.find_overlapping_tasks([]) == {}

    def test_pair_ordering(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-2", ["src/shared.py"])
        af_manager.set_files("task-1", ["src/shared.py"])
        overlaps = af_manager.find_overlapping_tasks(["task-2", "task-1"])
        # Pairs should be ordered lexicographically
        assert ("task-1", "task-2") in overlaps


class TestGetTasksForFile:
    def test_reverse_lookup(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files("task-1", ["src/shared.py"])
        af_manager.set_files("task-2", ["src/shared.py"])
        results = af_manager.get_tasks_for_file("src/shared.py")
        task_ids = {r.task_id for r in results}
        assert task_ids == {"task-1", "task-2"}

    def test_reverse_lookup_no_results(self, af_manager: TaskAffectedFileManager) -> None:
        results = af_manager.get_tasks_for_file("nonexistent.py")
        assert results == []


class TestToDict:
    def test_to_dict(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.add_file("task-1", "src/a.py", source="manual")
        files = af_manager.get_files("task-1")
        d = files[0].to_dict()
        assert d["task_id"] == "task-1"
        assert d["file_path"] == "src/a.py"
        assert d["annotation_source"] == "manual"
        assert "id" in d
        assert "created_at" in d
