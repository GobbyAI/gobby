"""Instruction identities are exact, independently loaded capability requirements."""

from __future__ import annotations

import pytest

from gobby.skills.instruction_requirements import (
    instruction_fetch_directive,
    instruction_is_loaded,
    parse_instruction_requirement,
)
from gobby.workflows.claimed_task_extra_skills import missing_claimed_task_extra_skills

pytestmark = pytest.mark.unit

REFERENCE = "gobby:references/tasks/closing.md"


@pytest.mark.parametrize(
    "value",
    [
        "",
        " gobby",
        "gobby ",
        "Gobby",
        "../gobby",
        "gobby:",
        "gobby:/references/tasks.md",
        "gobby:references/../tasks.md",
        "gobby:references//tasks.md",
        "gobby:references/./tasks.md",
        "gobby:references\\tasks.md",
        "gobby:SKILL.md",
        "gobby:references/tasks.txt",
        "gobby:references/tasks.md#closing",
        "gobby:references/%2e%2e/tasks.md",
        "gobby:references/task\n.md",
        "gobby:references/tasks:closing.md",
        "gobby:references/tasks.md?x=1",
    ],
)
def test_rejects_noncanonical_instruction_identities(value: str) -> None:
    with pytest.raises(ValueError):
        parse_instruction_requirement(value)
    assert not instruction_is_loaded(value, {"loaded_skill_references": [value]})


def test_parses_plain_names_and_exact_references() -> None:
    plain = parse_instruction_requirement("code-review")
    reference = parse_instruction_requirement(REFERENCE)
    assert (plain.skill, plain.path, plain.identity) == ("code-review", None, "code-review")
    assert (reference.skill, reference.path, reference.identity) == (
        "gobby",
        "references/tasks/closing.md",
        REFERENCE,
    )


@pytest.mark.parametrize(
    ("skills", "references", "expected"),
    [
        (["gobby"], [], False),
        ([REFERENCE], [], False),
        (["gobby"], ["gobby:references/tasks/overview.md"], False),
        ([], ["custom:references/tasks/closing.md"], False),
        ([], [REFERENCE], True),
        (["gobby"], [REFERENCE], True),
    ],
)
def test_reference_contract_1_1_2(skills: list[str], references: list[str], expected: bool) -> None:
    variables: dict[str, object] = {
        "loaded_skills": skills,
        "loaded_skill_references": references,
        "claimed_task_extra_skills": [REFERENCE],
    }
    assert instruction_is_loaded(REFERENCE, variables) is expected
    assert missing_claimed_task_extra_skills(variables) == ([] if expected else [REFERENCE])


def test_plain_skills_do_not_use_reference_ledger_or_change_levels() -> None:
    variables: dict[str, object] = {
        "loaded_skills": ["brevity"],
        "loaded_skill_references": ["restraint"],
        "brevity_level": "max",
    }
    assert instruction_is_loaded("brevity", variables)
    assert not instruction_is_loaded("restraint", variables)
    assert variables["brevity_level"] == "max"


@pytest.mark.parametrize("ledger", [None, "gobby", {REFERENCE: True}])
def test_non_list_ledgers_cannot_satisfy_requirements(ledger: object) -> None:
    assert not instruction_is_loaded(REFERENCE, {"loaded_skill_references": ledger})


def test_invalid_reference_directive_reports_repair_without_a_fetch() -> None:
    directive = instruction_fetch_directive("gobby:references/../secret.md")
    assert "Invalid instruction requirement" in directive
    assert "call_tool" not in directive
