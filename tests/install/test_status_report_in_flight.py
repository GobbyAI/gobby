from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROCEDURES = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/skills/gobby/references/agents/procedures"
)
ASSISTANT = PROCEDURES / "assistant.md"
REPORTER = PROCEDURES / "reporter.md"
FULL_REPORT = PROCEDURES / "reporter-full-report.md"
IN_FLIGHT_HEADING = "In-flight tasks: stage, time in stage, claim-to-close"


def _section(text: str, heading: str) -> str:
    """Return a level-2 section's body, up to the next level-2 heading."""
    _, found, rest = text.partition(f"\n## {heading}\n")
    assert found, f"missing section: {heading}"
    return rest.split("\n## ", 1)[0]


def _bullet(section: str, label: str) -> str:
    """Return one top-level bullet, from its label to the next bullet."""
    _, found, rest = section.partition(f"\n- {label}:")
    assert found, f"missing bullet: {label}"
    return rest.split("\n- ", 1)[0]


def test_assistant_full_and_delta_reports_require_in_flight_section() -> None:
    report = _section(ASSISTANT.read_text(encoding="utf-8"), "Report")

    assert "in-flight" in _bullet(report, "Full")
    assert "claim-to-close" in _bullet(report, "Full")
    assert "in-flight" in _bullet(report, "Delta")
    assert "claim-to-close" in _bullet(report, "Delta")


def test_full_report_scaffold_has_in_flight_slot() -> None:
    scaffold = FULL_REPORT.read_text(encoding="utf-8")

    assert "- In-flight tasks:" in scaffold
    assert "claim-to-close" in scaffold


def test_assistant_gather_names_the_in_flight_query_owner() -> None:
    gather = ASSISTANT.read_text(encoding="utf-8")

    assert IN_FLIGHT_HEADING in gather
    assert "run the two in-flight queries with `query_readonly`" in gather


def test_in_flight_recipe_names_query_seat_and_stage_sources() -> None:
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)

    assert "`query_readonly` is Assistant- and Orchestrator-only" in recipe
    assert "A Reporter seat cannot run them" in recipe
    assert "task_claim_intervals" in recipe
    assert "author_type = 'task_step'" in recipe
    assert "#25377" in recipe


def test_in_flight_queries_are_single_ascii_read_only_statements() -> None:
    recipe = _section(REPORTER.read_text(encoding="utf-8"), IN_FLIGHT_HEADING)
    blocks = recipe.split("```sql\n")[1:]

    assert len(blocks) == 2
    for block in blocks:
        statement = block.split("```", 1)[0].strip()
        assert statement.startswith(("SELECT", "WITH"))
        assert ";" not in statement
        assert statement.isascii()
