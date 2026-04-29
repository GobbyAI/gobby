"""Compile-only dry-run coverage for the lifecycle dispatch plan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.plans.parser import parse_plan
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = [pytest.mark.integration, pytest.mark.slow]

PLAN_PATH = (
    Path(__file__).resolve().parents[2] / ".gobby/plans/task-12725-lifecycle-dispatch-rev1.md"
)
if not PLAN_PATH.exists():
    pytest.skip(
        "required plan file .gobby/plans/task-12725-lifecycle-dispatch-rev1.md not found",
        allow_module_level=True,
    )


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def test_plan_12725_compiles_clean(
    service: ExpansionService,
    sample_project,
) -> None:
    parent_task = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle dispatch",
        task_type="epic",
    )
    doc = parse_plan(PLAN_PATH, parse_mode="expansion")
    spec = service.compile_plan_to_spec(doc, parent_task)

    assert spec["contract_plan"] is True
    assert spec["plan_id"] == "task-12725-lifecycle-dispatch-rev1"
    assert spec["deliverable_count"] == 32
    assert len(spec["tasks"]) == 74
    assert {phase["id"] for phase in spec["phases"]} == {
        "phase-p1",
        "phase-p2",
        "phase-p3",
    }

    manifest_by_source = {entry.source_section: entry for entry in doc.manifest_entries}
    impl_or_single_tasks = [
        task for task in spec["tasks"] if not task["title"].startswith(("[TEST]", "[REF]"))
    ]
    assert len(impl_or_single_tasks) == 32
    for task in impl_or_single_tasks:
        section_id = task["source_section_id"]
        entry = manifest_by_source[section_id]
        if entry.tdd:
            expected_title = f"[IMPL] {entry.title}"
        else:
            expected_title = entry.title
        assert task["title"] == expected_title, (
            f"title drift for {section_id}: manifest={entry.title!r}, compiled={task['title']!r}"
        )
        assert task["category"] == entry.category, section_id
        assert task["task_type"] == entry.task_type, section_id
        assert task["validation"] == entry.validation_criteria, section_id
        assert task["assigned_agent"] == entry.assigned_agent, section_id
        assert sorted(task["labels"]) == sorted(entry.labels), section_id

    assert spec["deferrals"] == []

    edges_by_caller: dict[str, set[str]] = {}
    for edge in spec["dependencies"]:
        edges_by_caller.setdefault(edge["task_id"], set()).add(edge["depends_on"])

    annotated_entries = [entry for entry in doc.manifest_entries if entry.depends_on]
    assert len(annotated_entries) == 24, (
        "plan-12725 must have exactly 24 depends-on annotated deliverables; "
        f"got {len(annotated_entries)}"
    )

    for entry in annotated_entries:
        caller_lead = (
            f"{entry.source_section}::test" if entry.tdd else f"{entry.source_section}::single"
        )
        for blocker_section in entry.depends_on:
            blocker_entry = manifest_by_source.get(blocker_section)
            assert blocker_entry is not None, (
                f"unresolved depends_on target: {entry.source_section} depends on "
                f"{blocker_section}, which is not a manifest entry"
            )
            blocker_terminal = (
                f"{blocker_section}::ref" if blocker_entry.tdd else f"{blocker_section}::single"
            )
            assert blocker_terminal in edges_by_caller.get(caller_lead, set()), (
                f"missing dependency edge: {caller_lead} -> {blocker_terminal} "
                f"from {entry.source_section} depends_on {blocker_section}"
            )
