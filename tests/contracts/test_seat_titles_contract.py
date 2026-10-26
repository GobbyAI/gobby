"""Seat-title contract: Josh's rules are written down, and lane wording stays out.

Contract: docs/contracts/seat-titles.md. The quotes below are Josh's 2026-10-25 words.
A future change that reintroduces task-bearing titles or lane wording fails here by name.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "docs/contracts/seat-titles.md"
SHARED = REPO / "src/gobby/install/shared"
LANE = re.compile(r"\blane\b", re.IGNORECASE)
JOSH_QUOTES = (
    "Panes should be named after the Agent Definition. "
    "Titles shouldn't contain task info at all anymore.",
    "tab titles should be the name of the runbook (e.g. Dev Team - #task_ref)",
    "Unless they're static panes like Admin, Support, Etc.",
    "we don't have lanes anymore",
)


def _lane_hits(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if LANE.search(line):
                hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    return hits


def test_contract_doc_records_joshs_title_rules() -> None:
    assert CONTRACT.is_file()
    text = CONTRACT.read_text(encoding="utf-8")

    for quote in JOSH_QUOTES:
        assert quote in text


def test_team_planning_takes_no_lane_input() -> None:
    definition = yaml.safe_load((SHARED / "workflows/pipelines/team-planning.yaml").read_text())

    assert [name for name in definition["inputs"] if "lane" in name.lower()] == []


def test_seat_runbooks_and_agent_definitions_have_no_lane_wording() -> None:
    paths = sorted(
        [
            *(SHARED / "workflows/pipelines").glob("*.yaml"),
            *(SHARED / "workflows/agents").glob("*.yaml"),
        ]
    )

    assert _lane_hits(paths) == []


def test_spawn_and_placement_code_has_no_lane_wording() -> None:
    assert _lane_hits(sorted((REPO / "src/gobby/agents").glob("*.py"))) == []


def test_refuse_task_outside_lane_rule_is_retired() -> None:
    assert not (SHARED / "workflows/rules/task-enforcement/refuse-task-outside-lane.yaml").exists()
