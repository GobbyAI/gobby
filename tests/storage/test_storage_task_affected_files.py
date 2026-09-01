"""Tests for TaskAffectedFileManager storage layer."""

from collections.abc import Iterator

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
# Lexicographic ordering TASK_1 < TASK_2 < TASK_3 is relied on by pair-ordering tests.
TASK_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
TASK_2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
TASK_3 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Create a fresh database with task rows for FK constraints."""
    database = temp_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "test-project"),
    )
    for tid in (TASK_1, TASK_2, TASK_3):
        database.execute(
            "INSERT INTO tasks "
            "(id, title, project_id, task_type, priority, validation_criteria, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                tid,
                f"Task {tid}",
                PROJECT_ID,
                "task",
                2,
                "Storage fixture task; behavior asserted by the test.",
            ),
        )
    yield database


@pytest.fixture
def af_manager(db: HubDatabase) -> TaskAffectedFileManager:
    return TaskAffectedFileManager(db)


class TestSetFiles:
    def test_set_files_creates_records(self, af_manager: TaskAffectedFileManager) -> None:
        results = af_manager.set_files(TASK_1, ["src/a.py", "src/b.py"])
        assert len(results) == 2
        assert {r.file_path for r in results} == {"src/a.py", "src/b.py"}
        assert all(r.annotation_source == "expansion" for r in results)

    def test_set_files_replaces_same_source(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/a.py", "src/b.py"], source="expansion")
        af_manager.set_files(TASK_1, ["src/c.py"], source="expansion")
        files = af_manager.get_files(TASK_1)
        assert [f.file_path for f in files] == ["src/c.py"]

    def test_set_files_preserves_other_sources(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/a.py"], source="expansion")
        af_manager.set_files(TASK_1, ["src/b.py"], source="manual")
        # Replace expansion files only
        af_manager.set_files(TASK_1, ["src/c.py"], source="expansion")
        files = af_manager.get_files(TASK_1)
        paths = {f.file_path for f in files}
        assert "src/b.py" in paths  # manual preserved
        assert "src/c.py" in paths  # new expansion
        assert "src/a.py" not in paths  # old expansion removed

    def test_set_files_conflict_does_not_poison_ambient_transaction(
        self, db: HubDatabase, af_manager: TaskAffectedFileManager
    ) -> None:
        af_manager.set_files(TASK_1, ["src/shared.py"], source="manual")

        with db.transaction() as conn:
            results = af_manager.set_files(
                TASK_1,
                ["src/shared.py", "src/new.py"],
                source="expansion",
            )
            conn.execute(
                "INSERT INTO task_affected_files (task_id, file_path, annotation_source) "
                "VALUES (%s, %s, %s)",
                (TASK_1, "src/after-conflict.py", "observed"),
            )

        assert [result.file_path for result in results] == ["src/new.py"]
        files = {item.file_path: item.annotation_source for item in af_manager.get_files(TASK_1)}
        assert files == {
            "src/after-conflict.py": "observed",
            "src/new.py": "expansion",
            "src/shared.py": "manual",
        }


class TestReplaceDeclaredFiles:
    def test_replaces_declared_scope_and_promotes_observed_overlap(
        self, af_manager: TaskAffectedFileManager
    ) -> None:
        af_manager.set_files(TASK_1, ["src/old-manual.py"], source="manual")
        af_manager.set_files(TASK_1, ["src/old-expansion.py"], source="expansion")
        af_manager.set_files(
            TASK_1,
            ["src/evidence.py", "src/promoted.py"],
            source="observed",
        )

        results = af_manager.replace_declared_files(
            TASK_1,
            ["src/new.py", "src/promoted.py", "src/new.py"],
        )

        assert [item.file_path for item in results] == ["src/new.py", "src/promoted.py"]
        assert all(item.annotation_source == "manual" for item in results)
        assert {
            item.file_path: item.annotation_source for item in af_manager.get_files(TASK_1)
        } == {
            "src/evidence.py": "observed",
            "src/new.py": "manual",
            "src/promoted.py": "manual",
        }

    def test_empty_replacement_clears_only_declared_scope(
        self, af_manager: TaskAffectedFileManager
    ) -> None:
        af_manager.set_files(TASK_1, ["src/manual.py"], source="manual")
        af_manager.set_files(TASK_1, ["src/expansion.py"], source="expansion")
        af_manager.set_files(TASK_1, ["src/evidence.py"], source="observed")

        assert af_manager.replace_declared_files(TASK_1, []) == []
        assert [
            (item.file_path, item.annotation_source) for item in af_manager.get_files(TASK_1)
        ] == [("src/evidence.py", "observed")]


class TestGetFiles:
    def test_get_files_empty(self, af_manager: TaskAffectedFileManager) -> None:
        files = af_manager.get_files(TASK_1)
        assert files == []

    def test_get_files_ordered_by_path(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/z.py", "src/a.py", "src/m.py"])
        files = af_manager.get_files(TASK_1)
        assert [f.file_path for f in files] == ["src/a.py", "src/m.py", "src/z.py"]


class TestAddFile:
    def test_add_file_success(self, af_manager: TaskAffectedFileManager) -> None:
        result = af_manager.add_file(TASK_1, "src/new.py")
        assert result is not None
        assert result.file_path == "src/new.py"
        assert result.annotation_source == "manual"

    def test_add_file_duplicate_returns_none(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.add_file(TASK_1, "src/a.py")
        result = af_manager.add_file(TASK_1, "src/a.py")
        assert result is None

    def test_add_file_custom_source(self, af_manager: TaskAffectedFileManager) -> None:
        result = af_manager.add_file(TASK_1, "src/obs.py", source="observed")
        assert result is not None
        assert result.annotation_source == "observed"


class TestRemoveFile:
    def test_remove_file_success(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.add_file(TASK_1, "src/a.py")
        assert af_manager.remove_file(TASK_1, "src/a.py") is True
        assert af_manager.get_files(TASK_1) == []

    def test_remove_file_not_found(self, af_manager: TaskAffectedFileManager) -> None:
        assert af_manager.remove_file(TASK_1, "nonexistent.py") is False


class TestFindOverlappingTasks:
    def test_no_overlap(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/a.py"])
        af_manager.set_files(TASK_2, ["src/b.py"])
        overlaps = af_manager.find_overlapping_tasks([TASK_1, TASK_2])
        assert overlaps == {}

    def test_single_overlap(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/a.py", "src/shared.py"])
        af_manager.set_files(TASK_2, ["src/b.py", "src/shared.py"])
        overlaps = af_manager.find_overlapping_tasks([TASK_1, TASK_2])
        assert len(overlaps) == 1
        pair = (TASK_1, TASK_2)
        assert pair in overlaps
        assert overlaps[pair] == ["src/shared.py"]

    def test_multiple_overlaps(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/a.py", "src/b.py"])
        af_manager.set_files(TASK_2, ["src/a.py", "src/b.py", "src/c.py"])
        overlaps = af_manager.find_overlapping_tasks([TASK_1, TASK_2])
        pair = (TASK_1, TASK_2)
        assert set(overlaps[pair]) == {"src/a.py", "src/b.py"}

    def test_three_way_overlap(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/shared.py"])
        af_manager.set_files(TASK_2, ["src/shared.py"])
        af_manager.set_files(TASK_3, ["src/shared.py"])
        overlaps = af_manager.find_overlapping_tasks([TASK_1, TASK_2, TASK_3])
        assert len(overlaps) == 3  # 3 pairs: (1,2), (1,3), (2,3)

    def test_fewer_than_two_tasks(self, af_manager: TaskAffectedFileManager) -> None:
        assert af_manager.find_overlapping_tasks([TASK_1]) == {}
        assert af_manager.find_overlapping_tasks([]) == {}

    def test_pair_ordering(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_2, ["src/shared.py"])
        af_manager.set_files(TASK_1, ["src/shared.py"])
        overlaps = af_manager.find_overlapping_tasks([TASK_2, TASK_1])
        # Pairs should be ordered lexicographically
        assert (TASK_1, TASK_2) in overlaps


class TestGetTasksForFile:
    def test_reverse_lookup(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.set_files(TASK_1, ["src/shared.py"])
        af_manager.set_files(TASK_2, ["src/shared.py"])
        results = af_manager.get_tasks_for_file("src/shared.py")
        task_ids = {r.task_id for r in results}
        assert task_ids == {TASK_1, TASK_2}

    def test_reverse_lookup_no_results(self, af_manager: TaskAffectedFileManager) -> None:
        results = af_manager.get_tasks_for_file("nonexistent.py")
        assert results == []


class TestToDict:
    def test_to_dict(self, af_manager: TaskAffectedFileManager) -> None:
        af_manager.add_file(TASK_1, "src/a.py", source="manual")
        files = af_manager.get_files(TASK_1)
        d = files[0].to_dict()
        assert d["task_id"] == TASK_1
        assert d["file_path"] == "src/a.py"
        assert d["annotation_source"] == "manual"
        assert "id" in d
        assert "created_at" in d
