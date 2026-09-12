"""Completed reference responses are independent of entrypoint delivery."""

from typing import Any

import pytest

from gobby.workflows.observer_mcp import _track_loaded_skill


@pytest.mark.unit
@pytest.mark.parametrize("wrapper_depth", [0, 1, 2])
def test_reference_contract_1_2_1(wrapper_depth: int) -> None:
    response: dict[str, Any] = {
        "success": True,
        "file": {
            "skill_name": "gobby",
            "path": "references/tasks/closing.md",
            "content": "Close after verification.",
        },
        "page": {"complete": True, "next_cursor": None},
    }
    for _ in range(wrapper_depth):
        response = {"success": True, "result": response}
    variables: dict[str, Any] = {"loaded_skills": ["brevity"], "brevity_level": "max"}

    for _ in range(2):
        _track_loaded_skill(variables, response, "session", reference=True)
        _track_loaded_skill(variables, response, "session")

    assert variables == {
        "loaded_skills": ["brevity"],
        "brevity_level": "max",
        "loaded_skill_references": ["gobby:references/tasks/closing.md"],
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        {"success": False},
        {"files": [{"path": "references/tasks/closing.md"}]},
        {"menu": "tasks"},
        {"file": {"skill_name": "gobby", "path": "references/tasks/closing.md"}},
        {
            "file": {"skill_name": "gobby", "path": "SKILL.md", "content": "Router"},
            "page": {"complete": True, "next_cursor": None},
        },
    ],
)
def test_non_reference_results_do_not_credit_completion(response: dict[str, Any]) -> None:
    variables: dict[str, Any] = {}
    _track_loaded_skill(variables, response, "session", reference=True)
    assert variables == {}


@pytest.mark.unit
@pytest.mark.parametrize("field", ["file", "skill"])
@pytest.mark.parametrize("complete,cursor", [(False, "next"), (True, "next"), (False, None)])
def test_partial_pages_are_not_loaded(field: str, complete: bool, cursor: str | None) -> None:
    response = {
        field: {
            "name": "gobby",
            "skill_name": "gobby",
            "path": "references/tasks/closing.md",
            "content": "partial",
        },
        "page": {"complete": complete, "next_cursor": cursor},
    }
    variables: dict[str, Any] = {}
    _track_loaded_skill(variables, response, "session", reference=field == "file")
    assert variables == {}
