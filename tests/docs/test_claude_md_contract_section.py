"""Content tests for the CLAUDE.md Plan-Coverage Contract section."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CLAUDE = Path("CLAUDE.md")
CONTRACT = Path("docs/contracts/plan-coverage.md")

CANONICAL_PLAN_HEADING_REGEX = (
    r"^#{2,6}\s+(?:§\s*)?"
    r"(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))"
    r"(?=\s|[).:-]|$)"
)


def _section() -> str:
    body = CLAUDE.read_text(encoding="utf-8")
    match = re.search(
        r"## Plan-Coverage Contract\n(?P<section>.*?)(?=\n## |\Z)",
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
    section = _section()
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
        "Bootstrap-ledger requirement",
        ".coverage-ledger.yaml",
        ".grandfathered",
        "# remove-by: <task-ref>",
        "Table-row decomposition rule",
    )
    for term in required_terms:
        assert term in section


def test_canonical_regex_pinned_in_claude_md() -> None:
    assert _first_regex_block(_section()) == CANONICAL_PLAN_HEADING_REGEX
    assert _first_regex_block(_section()) == _parser_heading_pattern()


def test_table_row_decomposition_rule_documented() -> None:
    for path in (CLAUDE, CONTRACT):
        body = path.read_text(encoding="utf-8").lower()
        assert "table-row decomposition" in body
        assert "one acceptance item per" in body
        assert "data row" in body
        assert "plan-adversary" in body
