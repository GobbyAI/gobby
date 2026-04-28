"""Plan-Coverage Contract content tests for bundled planning skills."""

import re
from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

CANONICAL_PLAN_HEADING_REGEX = (
    r"^#{2,6}\s+(?:§\s*)?"
    r"(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))"
    r"(?=\s|[).:-]|$)"
)

PLAN_DRAFT = Path("src/gobby/install/shared/skills/plan-draft/SKILL.md")
PLAN_REVIEW = Path("src/gobby/install/shared/skills/plan-review/SKILL.md")
PLANNER = Path("src/gobby/install/shared/workflows/agents/planner.yaml")
ADVERSARY = Path("src/gobby/install/shared/workflows/agents/plan-adversary.yaml")


def _first_regex_block(path: Path) -> str:
    body = path.read_text(encoding="utf-8")
    match = re.search(r"```regex\n(?P<pattern>.+?)\n```", body, re.DOTALL)
    assert match is not None, f"{path} has no regex code block"
    return match.group("pattern")


def _parser_heading_pattern() -> str:
    try:
        from gobby.plans.parser import PLAN_HEADING_REGEX
    except ModuleNotFoundError as exc:
        if exc.name in {"gobby.plans", "gobby.plans.parser"}:
            pytest.skip("A2 parser module is not present in this worktree yet")
        raise
    return PLAN_HEADING_REGEX.pattern


def _agent_prompt(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    agent = AgentDefinitionBody.model_validate(data)
    return agent.build_prompt_preamble() or ""


def test_canonical_regex_pinned() -> None:
    assert _first_regex_block(PLAN_DRAFT) == CANONICAL_PLAN_HEADING_REGEX
    assert _first_regex_block(PLAN_DRAFT) == _parser_heading_pattern()


def test_kind_enum_documented() -> None:
    body = PLAN_DRAFT.read_text(encoding="utf-8")
    assert "deliverable | framing | verification | deferred" in body
    for kind in ("deliverable", "framing", "verification", "deferred"):
        assert f"`{kind}`" in body
    assert "acceptance items" in body
    assert "typed deferral object" in body


def test_acceptance_item_shape_documented() -> None:
    body = PLAN_DRAFT.read_text(encoding="utf-8")
    assert "**Acceptance:**" in body
    assert "dotted suffix" in body
    for artifact_kind in ("file", "symbol", "test", "behavior"):
        assert f"`{artifact_kind}`" in body


def test_deferral_object_and_covers_record_documented() -> None:
    body = PLAN_DRAFT.read_text(encoding="utf-8")
    for field in ("task_ref", "reason", "owner", "original_acceptance_items"):
        assert field in body
    assert "deferred-from:<plan-id>:<section-id>" in body
    assert "covers:<plan-id>:<section-id>:<item-id>" in body


def test_table_row_decomposition_rule_documented() -> None:
    surfaces = {
        "plan-draft": PLAN_DRAFT.read_text(encoding="utf-8"),
        "plan-review": PLAN_REVIEW.read_text(encoding="utf-8"),
        "planner": _agent_prompt(PLANNER),
        "plan-adversary": _agent_prompt(ADVERSARY),
    }
    for name, body in surfaces.items():
        lowered = body.lower()
        normalized = re.sub(r"\s+", " ", lowered)
        snippet = lowered[:150]
        assert "table-row decomposition" in normalized, f"{name}: {snippet!r}"
        assert "one acceptance item per" in normalized, f"{name}: {snippet!r}"
        assert "data row" in normalized, f"{name}: {snippet!r}"
