"""Claimed-task extra skill projection and reload helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import psycopg

from gobby.skills.instruction_requirements import (
    instruction_is_loaded,
    parse_instruction_requirement,
)
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.acceptance_artifacts import extract_artifact_references, parse_test_reference
from gobby.tasks.tdd_evidence import TDD_SKILL, task_requires_tdd

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

EXTRA_SKILLS_VARIABLE = "claimed_task_extra_skills"
UNRESOLVABLE_EXTRA_SKILLS_VARIABLE = "unresolvable_claimed_task_extra_skills"
CLAIMED_TASK_REQUIRES_TDD_VARIABLE = "claimed_task_requires_tdd"
CLAIMED_TASK_ACCEPTANCE_TEST_PATHS_VARIABLE = "claimed_task_acceptance_test_paths"


def build_claimed_task_extra_skill_state(
    variables: dict[str, Any],
    task_manager: LocalTaskManager | None,
) -> dict[str, Any]:
    """Build ordered task extras for every task currently claimed by the session."""
    claimed_tasks = variables.get("claimed_tasks") or {}
    extras: list[str] = []
    requires_tdd = False
    acceptance_test_paths: list[str] = []

    if isinstance(claimed_tasks, dict) and task_manager is not None:
        for task_id in claimed_tasks:
            task = _load_task(task_manager, str(task_id))
            if task is None:
                continue

            labels = _string_list(_field(task, "labels"))
            additional_skills = _string_list(_field(task, "additional_skills"))
            validation_criteria = _string_field(task, "validation_criteria")

            _extend_unique(extras, additional_skills)
            task_needs_tdd = task_requires_tdd(
                labels=labels,
                additional_skills=additional_skills,
                validation_criteria=validation_criteria,
                enforce_tdd=bool(variables.get("enforce_tdd")),
            )
            if task_needs_tdd:
                requires_tdd = True
                _append_unique(extras, TDD_SKILL)

            if validation_criteria:
                for reference in extract_artifact_references(validation_criteria, "test"):
                    parsed = parse_test_reference(reference)
                    if parsed is not None:
                        _append_unique(acceptance_test_paths, parsed[0])

    unresolved = _string_list(variables.get(UNRESOLVABLE_EXTRA_SKILLS_VARIABLE))
    # A malformed identifier can never load, so it must not hold the extras gate.
    _extend_unique(unresolved, [skill for skill in extras if not _is_instruction(skill)])
    return {
        EXTRA_SKILLS_VARIABLE: extras,
        UNRESOLVABLE_EXTRA_SKILLS_VARIABLE: [skill for skill in unresolved if skill in extras],
        CLAIMED_TASK_REQUIRES_TDD_VARIABLE: requires_tdd,
        CLAIMED_TASK_ACCEPTANCE_TEST_PATHS_VARIABLE: acceptance_test_paths,
    }


def refresh_claimed_task_extra_skills(
    variables: dict[str, Any],
    task_manager: LocalTaskManager | None,
) -> dict[str, Any]:
    """Refresh claimed-task extras in-place and return the persisted merge."""
    merge = build_claimed_task_extra_skill_state(variables, task_manager)
    variables.update(merge)
    return merge


def missing_claimed_task_extra_skills(variables: dict[str, Any]) -> list[str]:
    """Return unloaded, resolvable extras in their declared order."""
    unresolved = set(_string_list(variables.get(UNRESOLVABLE_EXTRA_SKILLS_VARIABLE)))
    return [
        skill
        for skill in _string_list(variables.get(EXTRA_SKILLS_VARIABLE))
        if not instruction_is_loaded(skill, variables) and skill not in unresolved
    ]


def _is_instruction(skill: str) -> bool:
    try:
        parse_instruction_requirement(skill)
    except ValueError:
        return False
    return True


def _load_task(task_manager: LocalTaskManager, task_id: str) -> Any | None:
    try:
        return task_manager.get_task(task_id)
    except (TaskNotFoundError, ValueError, psycopg.Error) as exc:
        logger.debug("Failed to load claimed task %s for extra skills: %s", task_id, exc)
        return None


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


def _extend_unique(items: list[str], values: list[str]) -> None:
    for value in values:
        _append_unique(items, value)
