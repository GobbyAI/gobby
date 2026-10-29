"""Automatic expansion routing comes from declarations, never familiar names."""

from typing import Any

import pytest
from pydantic import ValidationError

from gobby.tasks.expansion._common import _agent_selection_fields
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "routing",
    [{"task_categories": ["unknown"]}, {"implementation_domains": ["mobile"]}],
)
def test_definition_rejects_unknown_route_values(routing: dict[str, list[str]]) -> None:
    with pytest.raises(ValidationError, match="Unsupported"):
        make_agent_definition(**routing)


@pytest.mark.parametrize("category", ["code", "config", "docs", "refactor", "test"])
def test_expansion_selects_declared_category(category: str) -> None:
    definitions: list[dict[str, Any]] = [
        {"name": "developer"},
        {"name": "tech-writer"},
        {"name": "custom-builder", "task_categories": [category]},
    ]
    agent, skills, description = _agent_selection_fields(
        {"category": category, "description": "Deliver the change", "additional_skills": ["x"]},
        definitions,
    )

    assert (agent, skills, description) == ("custom-builder", ["x"], "Deliver the change")


@pytest.mark.parametrize("domain", ["backend", "frontend", "fullstack"])
def test_expansion_uses_declared_domain(domain: str) -> None:
    definitions = [
        {
            "name": "server-builder",
            "task_categories": ["code"],
            "implementation_domains": ["backend"],
        },
        {"name": "ui-builder", "task_categories": ["code"], "implementation_domains": ["frontend"]},
        {
            "name": "mixed-builder",
            "task_categories": ["code"],
            "implementation_domains": ["fullstack"],
        },
    ]

    agent, _, _ = _agent_selection_fields(
        {"category": "code", "implementation_domain": domain}, definitions
    )

    assert (
        agent
        == {"backend": "server-builder", "frontend": "ui-builder", "fullstack": "mixed-builder"}[
            domain
        ]
    )


@pytest.mark.parametrize(
    "definitions",
    [
        [],
        [{"name": "developer"}, {"name": "tech-writer"}],
        [{"name": "off", "enabled": False, "task_categories": ["code"]}],
        [{"name": "interactive", "surfaces": ["persona"], "task_categories": ["code"]}],
        [{"name": "ui", "task_categories": ["code"], "implementation_domains": ["frontend"]}],
        [
            {"name": "one", "task_categories": ["code"]},
            {"name": "two", "task_categories": ["code"]},
        ],
    ],
)
def test_unresolved_routes_leave_assignment_open(definitions: list[dict[str, Any]]) -> None:
    agent, skills, description = _agent_selection_fields(
        {
            "category": "code",
            "implementation_domain": "backend",
            "additional_skills": ["test-driven-development"],
        },
        definitions,
    )

    assert agent is None
    assert skills == ["test-driven-development"]
    assert "## Agent Selection" in description
    assert "unassigned" in description.lower()


def test_explicit_available_assignment_resolves_ambiguous_routes() -> None:
    agent, _, _ = _agent_selection_fields(
        {"category": "code", "assigned_agent": "two"},
        [
            {"name": "one", "task_categories": ["code"]},
            {"name": "two", "task_categories": ["code"]},
        ],
    )

    assert agent == "two"


def test_domain_restrictions_only_apply_to_code_routes() -> None:
    agent, _, _ = _agent_selection_fields(
        {"category": "docs"},
        [{"name": "author", "task_categories": ["docs"], "implementation_domains": ["backend"]}],
    )

    assert agent == "author"
