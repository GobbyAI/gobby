"""Tests for claimed-task skill metadata helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.tdd_evidence import task_requires_tdd
from gobby.workflows.claimed_task_skills import (
    LANGUAGE_SKILL_EXTENSIONS,
    _append_unique_path,
    _language_skills_for_files,
    _load_task,
    _task_files,
    build_claimed_task_skill_state,
    missing_claimed_task_required_skills,
)

pytestmark = pytest.mark.unit

SKILL_DISCOVERY_RULES = (
    Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules/skill-discovery"
)

# These two require-*-skill rules gate on a path predicate rather than a file
# extension — require-plan-skill on the `.gobby/plans/` prefix and
# require-impeccable-skill on `touches_ui_design_path` — so neither can have an
# entry in an extension map. Every other rule blocks a write keyed on the
# file's extension and must be covered.
PATH_PREDICATE_SKILL_RULES = frozenset({"require-plan-skill", "require-impeccable-skill"})


def test_language_skill_extension_map_covers_installed_language_rules() -> None:
    installed_language_skills = {
        path.stem.removeprefix("require-").removesuffix("-skill")
        for path in SKILL_DISCOVERY_RULES.glob("require-*-skill.yaml")
        if path.stem not in PATH_PREDICATE_SKILL_RULES
    }

    assert set(LANGUAGE_SKILL_EXTENSIONS) == installed_language_skills


@pytest.mark.parametrize(
    ("file_path", "expected_skill"),
    [
        ("scripts/check.sh", "bash"),
        ("src/main.c", "c"),
        ("src/main.cpp", "cpp"),
        ("src/Program.cs", "csharp"),
        ("lib/app.dart", "dart"),
        ("lib/app.ex", "elixir"),
        ("cmd/server/main.go", "go"),
        ("src/Main.java", "java"),
        ("web/app.js", "javascript"),
        ("config/data.json", "json"),
        ("src/Main.kt", "kotlin"),
        ("lua/init.lua", "lua"),
        ("ios/ViewController.m", "objc"),
        ("public/index.php", "php"),
        ("src/module.py", "python"),
        ("app/models/user.rb", "ruby"),
        ("src/lib.rs", "rust"),
        ("src/Main.scala", "scala"),
        ("Sources/App.swift", "swift"),
        ("web/component.ts", "typescript"),
        ("config/app.yaml", "yaml"),
    ],
)
def test_language_skills_for_files_covers_bundled_language_skills(
    file_path: str,
    expected_skill: str,
) -> None:
    assert expected_skill in _language_skills_for_files([file_path])


def test_claimed_task_touching_typescript_requires_typescript_skill() -> None:
    task = SimpleNamespace(
        labels=[],
        additional_skills=[],
        validation_criteria=None,
        category="code",
    )
    manager = MagicMock()

    with (
        patch("gobby.workflows.claimed_task_skills._load_task", return_value=task),
        patch("gobby.workflows.claimed_task_skills._task_files", return_value=["web/app.ts"]),
    ):
        state = build_claimed_task_skill_state(
            {"claimed_tasks": {"task-id": {}}},
            manager,
        )

    assert "typescript" in state["claimed_task_language_skills"]
    assert "typescript" in state["claimed_task_required_skills"]


def test_every_claimed_task_requires_task_transitions_first() -> None:
    task = SimpleNamespace(
        labels=[],
        additional_skills=[],
        validation_criteria=None,
        category="docs",
    )
    manager = MagicMock()

    with (
        patch("gobby.workflows.claimed_task_skills._load_task", return_value=task),
        patch("gobby.workflows.claimed_task_skills._task_files", return_value=[]),
    ):
        state = build_claimed_task_skill_state(
            {"claimed_tasks": {"task-id": {}}},
            manager,
        )

    assert state["claimed_task_required_skills"] == ["tasks"]


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
                "created_at": "2026-01-01T00:00:00+00:00",
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
    assert task_requires_tdd(
        labels=(),
        additional_skills=(),
        validation_criteria="TDD evidence required: red, green, refactor/final-green.",
    )
    assert not task_requires_tdd(
        labels=(),
        additional_skills=(),
        validation_criteria="Redirection and evergreen refactoring notes are enough.",
    )


def test_missing_required_skills_skip_unresolvable_names() -> None:
    variables = {
        "claimed_task_required_skills": ["typo-skill", "python"],
        "unresolvable_required_skills": ["typo-skill"],
        "loaded_skills": [],
    }

    assert missing_claimed_task_required_skills(variables) == ["python"]

    variables["unresolvable_required_skills"].append("python")
    assert missing_claimed_task_required_skills(variables) == []


@pytest.mark.parametrize(
    ("file_path", "loaded_skills", "expected"),
    [
        ("/project/src/app.py", ["development-discipline"], ["python"]),
        ("/project/web/app.tsx", ["development-discipline"], ["typescript"]),
        ("/project/src/app.py", ["python"], ["development-discipline"]),
    ],
)
def test_missing_required_skills_scope_languages_to_touched_file(
    file_path: str,
    loaded_skills: list[str],
    expected: list[str],
) -> None:
    variables = {
        "claimed_task_required_skills": [
            "python",
            "typescript",
            "development-discipline",
        ],
        "claimed_task_language_skills": ["python", "typescript"],
        "loaded_skills": loaded_skills,
    }

    assert (
        missing_claimed_task_required_skills(
            variables,
            {"file_path": file_path},
            {"canonical_file_path": file_path},
        )
        == expected
    )


def test_missing_required_skills_include_each_touched_language_in_order() -> None:
    variables = {
        "claimed_task_required_skills": [
            "python",
            "typescript",
            "development-discipline",
        ],
        "claimed_task_language_skills": ["python", "typescript"],
        "loaded_skills": ["python", "development-discipline"],
    }

    assert missing_claimed_task_required_skills(
        variables,
        {"file_paths": ["/project/src/app.py", "/project/web/app.ts"]},
    ) == ["typescript"]


def test_missing_required_skills_fail_closed_for_opaque_write() -> None:
    variables = {
        "claimed_task_required_skills": [
            "python",
            "typescript",
            "development-discipline",
        ],
        "claimed_task_language_skills": ["python", "typescript"],
        "loaded_skills": [],
    }

    assert missing_claimed_task_required_skills(variables, {}, {}) == [
        "python",
        "typescript",
        "development-discipline",
    ]
