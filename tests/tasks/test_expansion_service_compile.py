"""Parser-driven task expansion compile tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.plans.parser import Kind, PlanDocument, parse_plan
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def _parent(service: ExpansionService, sample_project: dict[str, Any]) -> Task:
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle dispatch",
        task_type="epic",
    )


def _regression_plan_path() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures/plans/expansion-compile-regression.md"


def _regression_plan_doc() -> PlanDocument:
    return parse_plan(_regression_plan_path(), parse_mode="expansion")


def _deps_for(spec: dict[str, Any], task_id: str) -> set[str]:
    return {edge["depends_on"] for edge in spec["dependencies"] if edge["task_id"] == task_id}


def test_compile_contract_plan_emits_tdd_leaves_by_phase(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _regression_plan_doc()
    deliverable_count = sum(1 for section in plan_doc.sections if section.kind is Kind.deliverable)

    spec = service.compile_plan_to_spec(plan_doc, parent)

    assert len(plan_doc.manifest_entries) == deliverable_count
    assert spec["contract_plan"] is True
    assert spec["deliverable_count"] == 6
    assert len(spec["tasks"]) == deliverable_count
    assert {phase["id"]: len(phase["task_ids"]) for phase in spec["phases"]} == {
        "phase-p1": 3,
        "phase-p2": 2,
        "phase-p3": 1,
    }
    assert all(not any(key.startswith("tdd_") for key in phase) for phase in spec["phases"])
    assert spec["tdd_mode"] == "skill_backed"
    assert not any(
        task["title"].startswith(("[TEST]", "[REF]", "[IMPL]")) for task in spec["tasks"]
    )


def test_compile_contract_plan_emits_covers_labels_for_each_tdd_leaf(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _regression_plan_doc()
    plan_id = _regression_plan_path().stem
    expected_labels = {
        section.section_id: {
            f"covers:{plan_id}:{section.section_id}:{item.item_id}"
            for item in section.acceptance_items
        }
        for section in plan_doc.sections
        if section.kind is Kind.deliverable
    }

    spec = service.compile_plan_to_spec(plan_doc, parent)

    # Per-entry tasks carry their section's covers labels plus TDD metadata.
    for task in spec["tasks"]:
        section_id = task["source_section_id"]
        expected = set(expected_labels[section_id])
        if task["tdd_required"]:
            expected.add("tdd:required")
        assert set(task["labels"]) == expected
        assert expected_labels[section_id]


def test_compile_contract_plan_translates_section_dependencies(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_regression_plan_doc(), parent)

    # Cross-deliverable depends_on links single-task leaves directly.
    assert _deps_for(spec, "1.1::single") == set()
    assert _deps_for(spec, "1.2::single") == {"1.1::single"}
    assert "1.2::single" in _deps_for(spec, "1.3a::single")
    # 2.1 depends_on 1.3a (single).
    assert "1.3a::single" in _deps_for(spec, "2.1::single")
    # 2.2 depends_on 1.2.
    assert "1.2::single" in _deps_for(spec, "2.2::single")
    # 3.1 depends_on both 2.1 and 2.2.
    assert {"2.1::single", "2.2::single"} <= _deps_for(spec, "3.1::single")


def test_compile_contract_plan_uses_manifest_agent_assignment(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_regression_plan_doc(), parent)

    # Per-entry tasks carry the manifest's assigned_agent verbatim.
    section_tasks = [task for task in spec["tasks"] if task["source_section_id"] is not None]
    frontend_sections = {
        task["source_section_id"]
        for task in section_tasks
        if task["assigned_agent"] == "frontend-developer"
    }
    assert frontend_sections == {"2.1"}
    assert {
        task["assigned_agent"]
        for task in section_tasks
        if task["source_section_id"] not in frontend_sections
    } == {"backend-developer"}
    assert {
        task["source_section_id"]
        for task in section_tasks
        if task["additional_skills"] == ["test-driven-development"]
    } == {"1.1", "1.2", "2.1", "2.2"}
    assert all("[category:" not in task["title"] for task in spec["tasks"])
    assert all("(depends:" not in task["title"] for task in spec["tasks"])


def test_compile_assigns_agent_per_manifest_entry(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _regression_plan_doc()
    spec = service.compile_plan_to_spec(plan_doc, parent)

    expected_agents = {
        entry.source_section: entry.assigned_agent for entry in plan_doc.manifest_entries
    }

    assigned_by_section = {
        task["source_section_id"]: task["assigned_agent"]
        for task in spec["tasks"]
        if task["source_section_id"] is not None
    }

    assert assigned_by_section == expected_agents
    assert assigned_by_section["2.1"] == "frontend-developer"
    assert assigned_by_section["1.1"] == "backend-developer"
    assert all(task["assigned_agent"] for task in spec["tasks"])


def test_compile_contract_plan_prefers_manifest_assigned_agent_over_prose_regex(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = Path(__file__).resolve().parents[1] / "fixtures/plans/manifest-routing-bridge.md"
    spec = service.compile_plan_to_spec(parse_plan(plan_path, parse_mode="draft"), parent)

    # Section 2.1 emits one implementation leaf.
    section_tasks = [task for task in spec["tasks"] if task["source_section_id"] == "2.1"]
    assert len(section_tasks) == 1
    assert {task["assigned_agent"] for task in section_tasks} == {"backend-developer"}
    assert all(task["additional_skills"] == ["test-driven-development"] for task in section_tasks)


def test_compile_12898_contract_plan_preserves_manifest_deliverables(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = (
        Path(__file__).resolve().parents[2] / ".gobby/plans/task-12898-memory-recall-helper.md"
    )
    spec = service.compile_plan_to_spec(parse_plan(plan_path, parse_mode="expansion"), parent)

    assert spec["deliverable_count"] == 14
    assert len(spec["tasks"]) == 14
    assert len(spec["phases"]) == 3
    assert any(
        task["source_section_id"] == "2.6" and "notify_parent_on_completion" in task["title"]
        for task in spec["tasks"]
    )


def test_compile_rejects_missing_manifest_entry(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan = tmp_path / "missing-entry.md"
    plan.write_text(
        """
> **Plan ID:** missing-entry

## 1.1 Implement thing
`kind: deliverable`

**Acceptance:**
- 1.1.1 - Thing exists. file: `src/thing.py`

""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest entries") as excinfo:
        service.compile_plan_to_spec(parse_plan(plan, parse_mode="draft"), parent)

    assert "1.1" in str(excinfo.value)
