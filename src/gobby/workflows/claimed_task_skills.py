"""Claimed-task skill requirement aggregation."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import psycopg

from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.commits import extract_mentioned_files
from gobby.workflows.enforcement.blocking import is_source_code_path

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

PYTHON_SKILL = "python"
RUST_SKILL = "rust"
DEVELOPMENT_DISCIPLINE_SKILL = "development-discipline"
TDD_SKILL = "test-driven-development"
TDD_REQUIRED_LABEL = "tdd:required"
TDD_EVIDENCE_PHRASE = "tdd evidence"
TDD_CYCLE_KEYWORDS = frozenset({"red", "green", "refactor"})
TDD_FAILING_TEST_PHRASE = "failing test"
TDD_BEFORE_IMPLEMENTATION_PHRASE = "before implementation"

SOURCE_CODE_CATEGORIES = {"code", "refactor", "test"}
AGGREGATE_KEYS = (
    "claimed_task_required_skills",
    "claimed_task_language_skills",
    "claimed_task_labels",
    "claimed_task_additional_skills",
    "claimed_task_files",
    "claimed_task_validation_criteria",
)


def build_claimed_task_skill_state(
    variables: dict[str, Any],
    task_manager: LocalTaskManager | None,
) -> dict[str, Any]:
    """Build aggregate skill metadata for all currently claimed tasks."""
    claimed_tasks = variables.get("claimed_tasks") or {}
    if not isinstance(claimed_tasks, dict) or not claimed_tasks:
        return _empty_state()

    required_skills: list[str] = []
    language_skills: list[str] = []
    labels: list[str] = []
    additional_skills: list[str] = []
    files: list[str] = []
    validation_criteria: list[str] = []

    if task_manager is None:
        return _empty_state()

    for task_id in claimed_tasks:
        task = _load_task(task_manager, str(task_id))
        if task is None:
            continue

        task_labels = _string_list(_field(task, "labels"))
        task_additional_skills = _string_list(_field(task, "additional_skills"))
        task_validation_criteria = _string_field(task, "validation_criteria")
        task_files = _task_files(task, task_manager)
        task_language_skills = _language_skills_for_files(task_files)
        task_is_source = _task_is_source_code(task, task_files)

        _extend_unique(labels, task_labels)
        _extend_unique(additional_skills, task_additional_skills)
        _extend_unique(files, task_files)
        if task_validation_criteria:
            _append_unique(validation_criteria, task_validation_criteria)
        _extend_unique(language_skills, task_language_skills)
        _extend_unique(required_skills, task_language_skills)

        if task_is_source:
            _append_unique(required_skills, DEVELOPMENT_DISCIPLINE_SKILL)

        _extend_unique(required_skills, task_additional_skills)

        if _task_requires_tdd(
            labels=task_labels,
            additional_skills=task_additional_skills,
            validation_criteria=task_validation_criteria,
            enforce_tdd=bool(variables.get("enforce_tdd")),
        ):
            _append_unique(required_skills, TDD_SKILL)

    return {
        "claimed_task_required_skills": required_skills,
        "claimed_task_language_skills": language_skills,
        "claimed_task_labels": labels,
        "claimed_task_additional_skills": additional_skills,
        "claimed_task_files": files,
        "claimed_task_validation_criteria": validation_criteria,
    }


def refresh_claimed_task_skill_metadata(
    variables: dict[str, Any],
    task_manager: LocalTaskManager | None,
) -> dict[str, Any]:
    """Refresh claimed-task skill metadata in-place and return the merge dict."""
    merge = build_claimed_task_skill_state(variables, task_manager)
    variables.update(merge)
    return merge


def first_unloaded_claimed_task_required_skill(variables: dict[str, Any]) -> str:
    """Return the first required claimed-task skill not present in loaded_skills."""
    required = variables.get("claimed_task_required_skills") or []
    loaded = variables.get("loaded_skills") or []
    if not isinstance(required, list) or not isinstance(loaded, list):
        return ""

    loaded_set = {skill for skill in loaded if isinstance(skill, str)}
    for skill in required:
        if isinstance(skill, str) and skill and skill not in loaded_set:
            return skill
    return ""


def _empty_state() -> dict[str, list[str]]:
    return {key: [] for key in AGGREGATE_KEYS}


def _load_task(task_manager: LocalTaskManager, task_id: str) -> Any | None:
    try:
        return task_manager.get_task(task_id)
    except (TaskNotFoundError, ValueError, psycopg.Error) as e:
        logger.debug("Failed to load claimed task %s for skill metadata: %s", task_id, e)
        return None


def _task_files(task: Any, task_manager: LocalTaskManager) -> list[str]:
    files: list[str] = []
    task_id = _string_field(task, "id")
    if task_id:
        try:
            affected_files = TaskAffectedFileManager(task_manager.db).get_files(task_id)
        except psycopg.Error as e:
            logger.debug("Failed to load affected files for task %s: %s", task_id, e)
        else:
            for row in affected_files:
                file_path = _string_field(row, "file_path")
                if file_path:
                    _append_unique_path(files, file_path)

    payload = {
        "title": _string_field(task, "title"),
        "description": _string_field(task, "description"),
        "validation_criteria": _string_field(task, "validation_criteria"),
    }
    try:
        mentioned_files = extract_mentioned_files(payload)
    except Exception as e:
        logger.debug(
            "Failed to extract mentioned files for task %s from payload %s: %s",
            task_id,
            payload,
            e,
        )
        mentioned_files = []
    for file_path in mentioned_files:
        _append_unique_path(files, file_path)
    return files


def _language_skills_for_files(files: Iterable[str]) -> list[str]:
    skills: list[str] = []
    for file_path in files:
        if file_path.endswith(".py"):
            _append_unique(skills, PYTHON_SKILL)
        if file_path.endswith(".rs"):
            _append_unique(skills, RUST_SKILL)
    return skills


def _task_is_source_code(task: Any, files: list[str]) -> bool:
    category = _string_field(task, "category")
    if category in SOURCE_CODE_CATEGORIES:
        return True
    return any(is_source_code_path(file_path) for file_path in files)


def _task_requires_tdd(
    *,
    labels: list[str],
    additional_skills: list[str],
    validation_criteria: str | None,
    enforce_tdd: bool,
) -> bool:
    if enforce_tdd:
        return True
    if TDD_REQUIRED_LABEL in labels:
        return True
    if TDD_SKILL in additional_skills:
        return True
    return _criteria_require_tdd(validation_criteria)


def _criteria_require_tdd(validation_criteria: str | None) -> bool:
    if not validation_criteria:
        return False
    lowered = validation_criteria.lower()
    if TDD_SKILL in lowered or TDD_EVIDENCE_PHRASE in lowered:
        return True
    if all(_contains_word(lowered, keyword) for keyword in TDD_CYCLE_KEYWORDS):
        return True
    return TDD_FAILING_TEST_PHRASE in lowered and TDD_BEFORE_IMPLEMENTATION_PHRASE in lowered


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_field(value: Any, name: str) -> str | None:
    raw = _field(value, name)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _extend_unique(items: list[str], values: Iterable[str]) -> None:
    for value in values:
        _append_unique(items, value)


def _append_unique_path(paths: list[str], path: str) -> None:
    """Append path, deduping only exact normalized paths and bare filename aliases.

    Multi-segment paths are treated as distinct even when one is a suffix of
    another, so ``src/foo.py`` and ``tests/src/foo.py`` remain separate.
    """
    path_components = _path_components(path)
    if not path_components:
        return

    for index, existing in enumerate(paths):
        existing_components = _path_components(existing)
        if existing_components == path_components:
            return
        if _same_basename(existing_components, path_components):
            if len(existing_components) == 1 and len(path_components) > 1:
                paths[index] = path
            if len(path_components) == 1:
                return
            if len(existing_components) == 1:
                return

    paths.append(path)


def _contains_word(value: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", value) is not None


def _same_basename(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(left and right and left[-1] == right[-1])


def _path_components(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    return tuple(part for part in normalized.split("/") if part and part != ".")
