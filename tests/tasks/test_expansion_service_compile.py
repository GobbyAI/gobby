"""Parser-driven task expansion compile tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.plans.parser import Kind, parse_plan
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def _parent(service: ExpansionService, sample_project):
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle dispatch",
        task_type="epic",
    )


def _canonical_plan_path() -> Path:
    # This fixture intentionally tracks the canonical lifecycle-dispatch plan
    # because these tests verify the parser-driven expansion shape for that plan.
    return Path(__file__).resolve().parents[2] / ".gobby/plans/task-12725-lifecycle-dispatch.md"


def _deps_for(spec: dict, task_id: str) -> set[str]:
    return {edge["depends_on"] for edge in spec["dependencies"] if edge["task_id"] == task_id}


def test_compile_contract_plan_emits_tdd_leaves_by_phase(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = parse_plan(_canonical_plan_path())

    spec = service.compile_plan_to_spec(plan_doc, parent)

    assert spec["contract_plan"] is True
    assert spec["deliverable_count"] == 14
    assert len(spec["tasks"]) == 42
    assert {phase["id"]: len(phase["task_ids"]) for phase in spec["phases"]} == {
        "phase-p1": 27,
        "phase-p2": 12,
        "phase-p3": 3,
    }
    assert all(phase["tdd_sandwich_emitted"] is True for phase in spec["phases"])


def test_compile_contract_plan_emits_covers_labels_for_each_tdd_leaf(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = parse_plan(_canonical_plan_path())
    plan_id = _canonical_plan_path().stem
    expected_labels = {
        section.section_id: {
            f"covers:{plan_id}:{section.section_id}:{item.item_id}"
            for item in section.acceptance_items
        }
        for section in plan_doc.sections
        if section.kind is Kind.deliverable
    }

    spec = service.compile_plan_to_spec(plan_doc, parent)

    for task in spec["tasks"]:
        section_id = task["source_section_id"]
        assert set(task["labels"]) == expected_labels[section_id]
        assert expected_labels[section_id]


def test_compile_contract_plan_translates_section_dependencies(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(parse_plan(_canonical_plan_path()), parent)

    assert _deps_for(spec, "1.3a::test") == {"1.3::ref"}
    assert _deps_for(spec, "1.3a::impl") == {"1.3a::test"}
    assert _deps_for(spec, "1.3a::ref") == {"1.3a::impl"}
    assert _deps_for(spec, "1.9::test") == {
        "1.4::ref",
        "1.6::ref",
        "1.7::ref",
        "1.8::ref",
    }
    assert _deps_for(spec, "2.10::test") == {"1.8::ref"}


def test_compile_contract_plan_uses_deterministic_agent_assignment(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(parse_plan(_canonical_plan_path()), parent)

    frontend_sections = {
        task["source_section_id"]
        for task in spec["tasks"]
        if task["assigned_agent"] == "frontend-developer"
    }
    assert frontend_sections == {"2.8", "3.2"}
    assert {
        task["assigned_agent"]
        for task in spec["tasks"]
        if task["source_section_id"] not in frontend_sections
    } == {"backend-developer"}
    assert all(task["additional_skills"] == [] for task in spec["tasks"])
    assert all("[category:" not in task["title"] for task in spec["tasks"])
    assert all("(depends:" not in task["title"] for task in spec["tasks"])


def test_compile_12898_contract_plan_accepts_manual_deliverable(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = (
        Path(__file__).resolve().parents[2] / ".gobby/plans/task-12898-memory-recall-helper.md"
    )
    spec = service.compile_plan_to_spec(parse_plan(plan_path), parent)

    assert len(spec["tasks"]) == 39
    assert any(task["category"] == "manual" for task in spec["tasks"])
    assert service.validate_compiled_spec(spec)["valid"] is True
