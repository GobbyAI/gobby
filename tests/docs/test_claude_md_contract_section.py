"""Content tests for the AGENTS.md plans pointer and the Plan-Coverage Contract."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

AGENTS = Path("AGENTS.md")
CONTRACT = Path("docs/contracts/plan-coverage.md")

CANONICAL_PLAN_HEADING_REGEX = (
    r"^#{2,6}\s+(?:§\s*)?"
    r"(?P<section_id>(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?)"
    r"(?:\.(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?))*)"
    r"(?=\s|[).:-]|$)"
)


def _section() -> str:
    body = AGENTS.read_text(encoding="utf-8")
    match = re.search(
        r"## Plans\n(?P<section>.*?)(?=\n## |\Z)",
        body,
        re.DOTALL,
    )
    assert match is not None
    return match.group("section")


def _first_regex_block(text: str) -> str:
    match = re.search(r"```regex\n(?P<pattern>.+?)\n```", text, re.DOTALL)
    assert match is not None
    return match.group("pattern")


def _parser_heading_pattern() -> str:
    try:
        from gobby.plans.parser import PLAN_HEADING_REGEX
    except ModuleNotFoundError as exc:
        if exc.name in {"gobby.plans", "gobby.plans.parser"}:
            pytest.skip("A2 parser module is not present in this worktree yet")
        raise
    return PLAN_HEADING_REGEX.pattern


def test_plan_coverage_section_present() -> None:
    section = CONTRACT.read_text(encoding="utf-8")
    required_terms = (
        CANONICAL_PLAN_HEADING_REGEX,
        "deliverable | framing | verification | deferred",
        "A<section>.<n>",
        "file",
        "symbol",
        "test",
        "behavior",
        "task_ref",
        "reason",
        "owner",
        "original_acceptance_items",
        "deferred-from:<plan-id>:<section-id>",
        "covers:<plan-id>:<section-id>:<item-id>",
        "plan-ref:",
        "not honored",
        "gobby plan coverage",
        "--plan",
        "--plan-id",
        "--plan-hash",
        "--task-tree",
        "--root-task",
        "--project-id",
        "--matrix-file",
        "--evidence",
        "--manifest",
        "--regenerate",
        "`0`",
        "`2`",
        "`3`",
        "`4`",
        "`5`",
        "`6`",
        "`7`",
        "`8`",
        "commits | task-diff | worktree-diff | coverage-matrix | none",
        "`plans` table",
        "gobby-plans",
        "gobby plans",
        "implementation",
        "strategy",
        "active",
        "archived",
        "## Table-Row Decomposition",
    )
    for term in required_terms:
        assert term in section


def test_agents_md_points_to_plan_coverage_contract() -> None:
    section = _section()

    assert "docs/contracts/plan-coverage.md" in section
    assert "src/gobby/install/shared/skills/plan-draft/SKILL.md" in section


def test_canonical_regex_pinned_in_contract() -> None:
    section = CONTRACT.read_text(encoding="utf-8")
    block = _first_regex_block(section)
    assert block == CANONICAL_PLAN_HEADING_REGEX
    assert block == _parser_heading_pattern()


def test_table_row_decomposition_rule_documented() -> None:
    body = CONTRACT.read_text(encoding="utf-8").lower()
    assert "table-row decomposition" in body
    assert "one acceptance item per" in body
    assert "data row" in body
    assert "plan-adversary" in body


def test_no_retired_plan_storage_terms() -> None:
    stale_terms = (
        "index" + ".yaml",
        ".grand" + "fathered",
        ".legacy" + "-classification",
        "plan_kind` " + "—" + " one of `implementation`, `strategy`, `" + "legacy" + "`",
        "`status` " + "—" + " one of `active`, `" + "merged" + "`, `archived`",
    )
    for path in (AGENTS, CONTRACT):
        body = path.read_text(encoding="utf-8")
        for term in stale_terms:
            assert term not in body
