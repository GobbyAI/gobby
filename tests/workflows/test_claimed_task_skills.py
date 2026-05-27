"""Tests for claimed-task skill metadata helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.storage.tasks import TaskNotFoundError
from gobby.workflows.claimed_task_skills import (
    _append_unique_path,
    _criteria_require_tdd,
    _load_task,
    _task_files,
)

pytestmark = pytest.mark.unit


class _FakeAffectedFilesDb:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.fetchall_params: list[tuple[str, ...]] = []

    def fetchall(self, _sql: str, params: tuple[str, ...]) -> list[dict[str, object]]:
        self.fetchall_params.append(params)
        if self.error is not None:
            raise self.error
        return self.rows


def test_append_unique_path_dedupes_exact_normalized_path() -> None:
    paths = ["src/foo.py"]

    _append_unique_path(paths, "./src/foo.py")

    assert paths == ["src/foo.py"]


def test_append_unique_path_replaces_basename_with_specific_path() -> None:
    paths = ["foo.py"]

    _append_unique_path(paths, "src/foo.py")

    assert paths == ["src/foo.py"]


def test_append_unique_path_ignores_basename_when_specific_path_exists() -> None:
    paths = ["src/foo.py"]

    _append_unique_path(paths, "foo.py")

    assert paths == ["src/foo.py"]


def test_append_unique_path_keeps_distinct_basename_collisions() -> None:
    paths = ["src/foo.py"]

    _append_unique_path(paths, "tests/foo.py")

    assert paths == ["src/foo.py", "tests/foo.py"]


def test_append_unique_path_keeps_distinct_multi_segment_suffix_paths() -> None:
    paths = ["src/foo.py"]

    _append_unique_path(paths, "tests/src/foo.py")

    assert paths == ["src/foo.py", "tests/src/foo.py"]


def test_task_files_continues_when_extract_mentioned_files_raises() -> None:
    db = _FakeAffectedFilesDb(
        rows=[
            {
                "id": 1,
                "task_id": "task-1",
                "file_path": "src/from-db.py",
                "annotation_source": "manual",
                "created_at": "",
            }
        ]
    )
    task = SimpleNamespace(
        id="task-1",
        title="Update src/from-title.py",
        description=None,
        validation_criteria=None,
    )
    task_manager = SimpleNamespace(db=db)

    with patch(
        "gobby.workflows.claimed_task_skills.extract_mentioned_files",
        side_effect=RuntimeError("parser failed"),
    ) as extract_mentioned_files:
        assert _task_files(task, task_manager) == ["src/from-db.py"]
        assert db.fetchall_params == [("task-1",)]
        extract_mentioned_files.assert_called_once_with(
            {
                "title": "Update src/from-title.py",
                "description": None,
                "validation_criteria": None,
            }
        )


def test_task_files_swallows_expected_affected_file_database_error() -> None:
    db = _FakeAffectedFilesDb(error=psycopg.OperationalError("db down"))
    task = SimpleNamespace(
        id="task-1",
        title="Update src/from-title.py",
        description=None,
        validation_criteria=None,
    )
    task_manager = SimpleNamespace(db=db)

    with patch(
        "gobby.workflows.claimed_task_skills.extract_mentioned_files",
        return_value=["src/from-text.py"],
    ) as extract_mentioned_files:
        assert _task_files(task, task_manager) == ["src/from-text.py"]
        assert db.fetchall_params == [("task-1",)]
        extract_mentioned_files.assert_called_once_with(
            {
                "title": "Update src/from-title.py",
                "description": None,
                "validation_criteria": None,
            }
        )


def test_task_files_propagates_unexpected_affected_file_error() -> None:
    db = _FakeAffectedFilesDb(error=RuntimeError("unexpected"))
    task = SimpleNamespace(
        id="task-1",
        title="Update src/from-title.py",
        description=None,
        validation_criteria=None,
    )
    task_manager = SimpleNamespace(db=db)

    with pytest.raises(RuntimeError, match="unexpected"):
        _task_files(task, task_manager)
    assert db.fetchall_params == [("task-1",)]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("missing"),
        TaskNotFoundError("missing"),
        psycopg.OperationalError("db down"),
    ],
)
def test_load_task_swallows_expected_lookup_and_database_errors(error: Exception) -> None:
    task_manager = MagicMock()
    task_manager.get_task.side_effect = error

    assert _load_task(task_manager, "task-1") is None


def test_load_task_propagates_unexpected_errors() -> None:
    task_manager = MagicMock()
    task_manager.get_task.side_effect = KeyError("bad shape")

    with pytest.raises(KeyError, match="bad shape"):
        _load_task(task_manager, "task-1")


def test_criteria_require_tdd_matches_cycle_keywords_as_whole_words() -> None:
    assert _criteria_require_tdd("TDD evidence required: red, green, refactor/final-green.")
    assert not _criteria_require_tdd("Redirection and evergreen refactoring notes are enough.")
