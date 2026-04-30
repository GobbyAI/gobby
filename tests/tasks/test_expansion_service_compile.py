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
    # Per-phase sandwich: phase-p1 = TEST + 1.1::impl + 1.2::impl + REF + 1.3a::single = 5
    # phase-p2 = TEST + 2.1::impl + 2.2::impl + REF = 4
    # phase-p3 = TEST + 3.1::impl + REF = 3
    assert len(spec["tasks"]) == 12
    assert {phase["id"]: len(phase["task_ids"]) for phase in spec["phases"]} == {
        "phase-p1": 5,
        "phase-p2": 4,
        "phase-p3": 3,
    }
    assert all(phase["tdd_sandwich_emitted"] is True for phase in spec["phases"])

    # Each phase emits exactly one phase-level [TEST] and one [REF] task.
    test_titles_by_phase: dict[str, list[str]] = {}
    ref_titles_by_phase: dict[str, list[str]] = {}
    for task in spec["tasks"]:
        if task["title"].startswith("[TEST] Phase"):
            test_titles_by_phase.setdefault(task["phase_id"], []).append(task["title"])
        elif task["title"].startswith("[REF] Phase"):
            ref_titles_by_phase.setdefault(task["phase_id"], []).append(task["title"])
    assert {phase: len(titles) for phase, titles in test_titles_by_phase.items()} == {
        "phase-p1": 1,
        "phase-p2": 1,
        "phase-p3": 1,
    }
    assert {phase: len(titles) for phase, titles in ref_titles_by_phase.items()} == {
        "phase-p1": 1,
        "phase-p2": 1,
        "phase-p3": 1,
    }


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

    # Per-entry [IMPL]/single tasks carry their section's covers labels exactly.
    # Phase-level [TEST]/[REF] tasks have source_section_id=None and carry the
    # union of their phase's TDD entries' labels — verify they're a superset.
    phase_label_unions: dict[str, set[str]] = {}
    for task in spec["tasks"]:
        section_id = task["source_section_id"]
        if section_id is not None:
            assert set(task["labels"]) == expected_labels[section_id]
            assert expected_labels[section_id]
        else:
            phase_label_unions.setdefault(task["phase_id"], set()).update(task["labels"])

    # phase-p1 sandwich aggregates 1.1 + 1.2 covers labels (TDD entries only —
    # 1.3a is non-TDD and emitted as a single task outside the sandwich).
    assert phase_label_unions["phase-p1"] == (
        expected_labels["1.1"] | expected_labels["1.2"]
    )
    assert phase_label_unions["phase-p2"] == (
        expected_labels["2.1"] | expected_labels["2.2"]
    )
    assert phase_label_unions["phase-p3"] == expected_labels["3.1"]


def test_compile_contract_plan_translates_section_dependencies(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_regression_plan_doc(), parent)

    # Within-phase TDD sandwich edges: each [IMPL] depends on its phase [TEST];
    # phase [REF] depends on each [IMPL].
    assert _deps_for(spec, "1.1::impl") == {"phase-p1::__test"}
    assert _deps_for(spec, "1.2::impl") == {"phase-p1::__test", "1.1::impl"}
    assert {"1.1::impl", "1.2::impl"} <= _deps_for(spec, "phase-p1::__ref")

    # Cross-phase chain: phase N+1's [TEST] depends on phase N's [REF].
    assert "phase-p1::__ref" in _deps_for(spec, "phase-p2::__test")
    assert "phase-p2::__ref" in _deps_for(spec, "phase-p3::__test")

    # Cross-deliverable depends_on links IMPL/single → IMPL/single.
    # 1.3a (non-TDD) depends_on 1.2 → single depends on impl.
    assert "1.2::impl" in _deps_for(spec, "1.3a::single")
    # 2.1 depends_on 1.3a (single).
    assert "1.3a::single" in _deps_for(spec, "2.1::impl")
    # 2.2 depends_on 1.2.
    assert "1.2::impl" in _deps_for(spec, "2.2::impl")
    # 3.1 depends_on both 2.1 and 2.2.
    assert {"2.1::impl", "2.2::impl"} <= _deps_for(spec, "3.1::impl")


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
    assert all(task["additional_skills"] == [] for task in spec["tasks"])
    assert all("[category:" not in task["title"] for task in spec["tasks"])
    assert all("(depends:" not in task["title"] for task in spec["tasks"])


def test_compile_assigns_agent_per_manifest_entry(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_regression_plan_doc(), parent)

    assigned_by_section = {
        task["source_section_id"]: task["assigned_agent"]
        for task in spec["tasks"]
        if task["source_section_id"] is not None
    }

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

    # Per-phase sandwich: section 2.1 emits one [IMPL] task.
    section_tasks = [task for task in spec["tasks"] if task["source_section_id"] == "2.1"]
    assert len(section_tasks) == 1
    assert {task["assigned_agent"] for task in section_tasks} == {"backend-developer"}
    assert all(task["additional_skills"] == [] for task in section_tasks)


def test_compile_12898_contract_plan_requires_manifest(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = (
        Path(__file__).resolve().parents[2] / ".gobby/plans/task-12898-memory-recall-helper.md"
    )
    with pytest.raises(ValueError, match="kind: deliverable sections without manifest entries"):
        service.compile_plan_to_spec(parse_plan(plan_path, parse_mode="draft"), parent)


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

    with pytest.raises(ValueError, match="manifest entries"):
        service.compile_plan_to_spec(parse_plan(plan, parse_mode="draft"), parent)
