"""Tests for claimed-task extra-skill projection and reload helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.storage.tasks import TaskNotFoundError
from gobby.workflows.claimed_task_extra_skills import (
    _load_task,
    build_claimed_task_extra_skill_state,
    missing_claimed_task_extra_skills,
    refresh_claimed_task_extra_skills,
)

pytestmark = pytest.mark.unit


def _task(
    *,
    additional_skills: list[str] | None = None,
    labels: list[str] | None = None,
    validation_criteria: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        additional_skills=additional_skills or [],
        labels=labels or [],
        validation_criteria=validation_criteria,
    )


def test_explicit_extras_keep_order_and_precede_inferred_tdd() -> None:
    manager = MagicMock()
    manager.get_task.return_value = _task(
        additional_skills=["python", "context7", "python"],
        labels=["tdd:required"],
    )

    state = build_claimed_task_extra_skill_state(
        {"claimed_tasks": {"task-1": "#1"}},
        manager,
    )

    assert state["claimed_task_extra_skills"] == [
        "python",
        "context7",
        "test-driven-development",
    ]


def test_claim_exposes_tdd_gate_state() -> None:
    manager = MagicMock()
    tasks = {
        "task-1": _task(
            labels=["tdd:required"],
            validation_criteria=(
                "test: tests/workflows/test_claimed_task_extra_skills.py::test_one.\n"
                "test: tests/workflows/test_shared.py::test_shared."
            ),
        ),
        "task-2": _task(
            additional_skills=["test-driven-development"],
            validation_criteria=(
                "test: tests/workflows/test_shared.py::test_shared.\n"
                "test: crates/gclient/tests/parity/sidebar.rs::test_sidebar."
            ),
        ),
    }
    manager.get_task.side_effect = tasks.__getitem__

    state = build_claimed_task_extra_skill_state(
        {"claimed_tasks": {"task-1": "#1", "task-2": "#2"}},
        manager,
    )

    assert state == {
        "claimed_task_extra_skills": ["test-driven-development"],
        "unresolvable_claimed_task_extra_skills": [],
        "claimed_task_requires_tdd": True,
        "claimed_task_acceptance_test_paths": [
            "tests/workflows/test_claimed_task_extra_skills.py",
            "tests/workflows/test_shared.py",
            "crates/gclient/tests/parity/sidebar.rs",
        ],
    }

    skill_only_state = build_claimed_task_extra_skill_state(
        {"claimed_tasks": {"task-2": "#2"}},
        manager,
    )
    assert skill_only_state["claimed_task_requires_tdd"] is True
    assert skill_only_state["claimed_task_acceptance_test_paths"] == [
        "tests/workflows/test_shared.py",
        "crates/gclient/tests/parity/sidebar.rs",
    ]

    stale_variables = {
        "claimed_tasks": {},
        "claimed_task_requires_tdd": True,
        "claimed_task_acceptance_test_paths": ["tests/stale.py"],
    }
    assert refresh_claimed_task_extra_skills(stale_variables, manager) == {
        "claimed_task_extra_skills": [],
        "unresolvable_claimed_task_extra_skills": [],
        "claimed_task_requires_tdd": False,
        "claimed_task_acceptance_test_paths": [],
    }
    assert stale_variables["claimed_task_requires_tdd"] is False
    assert stale_variables["claimed_task_acceptance_test_paths"] == []


def test_multiple_claims_dedupe_without_reordering() -> None:
    manager = MagicMock()
    tasks = {
        "task-1": _task(additional_skills=["yaml", "shared"]),
        "task-2": _task(additional_skills=["shared", "json"]),
    }
    manager.get_task.side_effect = tasks.__getitem__

    state = build_claimed_task_extra_skill_state(
        {"claimed_tasks": {"task-1": "#1", "task-2": "#2"}},
        manager,
    )

    assert state["claimed_task_extra_skills"] == ["yaml", "shared", "json"]


@pytest.mark.parametrize(
    ("variables", "task", "expected"),
    [
        pytest.param({}, _task(labels=["tdd:required"]), True, id="label"),
        pytest.param(
            {},
            _task(validation_criteria="TDD evidence: red, green, refactor/final-green."),
            True,
            id="criteria",
        ),
        pytest.param({"enforce_tdd": True}, _task(), True, id="session-policy"),
        pytest.param(
            {},
            _task(validation_criteria="Implement src/app.py and config/app.yaml."),
            False,
            id="file-mentions-do-not-infer-languages",
        ),
    ],
)
def test_tdd_is_the_only_inferred_extra(
    variables: dict[str, object],
    task: SimpleNamespace,
    expected: bool,
) -> None:
    manager = MagicMock()
    manager.get_task.return_value = task
    variables["claimed_tasks"] = {"task-1": "#1"}

    extras = build_claimed_task_extra_skill_state(variables, manager)["claimed_task_extra_skills"]

    assert ("test-driven-development" in extras) is expected


def test_missing_extras_skip_loaded_and_unresolvable_names() -> None:
    variables = {
        "claimed_task_extra_skills": ["context7", "typo-skill", "python"],
        "loaded_skills": ["python"],
        "unresolvable_claimed_task_extra_skills": ["typo-skill"],
    }

    assert missing_claimed_task_extra_skills(variables) == ["context7"]


def test_refresh_state_drops_unresolvable_names_after_last_claim() -> None:
    state = build_claimed_task_extra_skill_state(
        {
            "claimed_tasks": {},
            "unresolvable_claimed_task_extra_skills": ["typo-skill"],
        },
        MagicMock(),
    )

    assert state == {
        "claimed_task_extra_skills": [],
        "unresolvable_claimed_task_extra_skills": [],
        "claimed_task_requires_tdd": False,
        "claimed_task_acceptance_test_paths": [],
    }


def test_malformed_extra_is_unresolvable() -> None:
    manager = MagicMock()
    manager.get_task.return_value = _task(additional_skills=["../tasks", "python"])

    state = build_claimed_task_extra_skill_state({"claimed_tasks": {"task-1": "#1"}}, manager)

    assert state["unresolvable_claimed_task_extra_skills"] == ["../tasks"]
    assert missing_claimed_task_extra_skills(state) == ["python"]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("missing"),
        TaskNotFoundError("missing"),
        psycopg.OperationalError("db down"),
    ],
)
def test_load_task_swallows_expected_lookup_errors(error: Exception) -> None:
    manager = MagicMock()
    manager.get_task.side_effect = error

    assert _load_task(manager, "task-1") is None


def test_load_task_propagates_unexpected_errors() -> None:
    manager = MagicMock()
    manager.get_task.side_effect = KeyError("bad shape")

    with pytest.raises(KeyError, match="bad shape"):
        _load_task(manager, "task-1")
