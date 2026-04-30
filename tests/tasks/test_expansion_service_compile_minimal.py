"""Minimal parser-driven expansion compile fixtures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.plans.parser import parse_plan
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def _write_minimal_plan(path: Path) -> Path:
    path.write_text(
        """> **Plan ID:** minimal-contract

# Minimal Contract Plan

## P1 Phase 1
`kind: framing`

### 1.1 Foundation [category: code]
`kind: deliverable`

Implement the first behavior.

**Acceptance:**
- 1.1.1 - Foundation exists. file: `src/foundation.py`

## P2 Phase 2
`kind: framing`

### 2.1 Follow-up [category: docs] (depends: 1.1)
`kind: deliverable`

Document the behavior.

**Acceptance:**
- 2.1.1 - Documentation exists. file: `docs/foundation.md`

### 2.2 Deferred work
`kind: deferred`

```yaml
task_ref: "#99"
reason: "covered by downstream follow-up"
owner: "docs"
original_acceptance_items:
  - item_id: 2.2.1
    prose: "Deferred behavior is tracked. behavior: downstream lifecycle"
    artifact_kind: behavior
    artifact_ref: "downstream lifecycle"
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "Foundation"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Foundation acceptance is satisfied."
  labels:
    - "covers:minimal-contract:1.1:1.1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.1"
- title: "Follow-up"
  category: docs
  task_type: task
  depends_on:
    - "1.1"
  validation_criteria: "Documentation acceptance is satisfied."
  labels:
    - "covers:minimal-contract:2.1:2.1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.1"
```
""",
        encoding="utf-8",
    )
    return path


def test_compile_minimal_contract_plan_with_cross_phase_dep_and_deferral(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Minimal epic",
        task_type="epic",
    )
    plan_doc = parse_plan(
        _write_minimal_plan(tmp_path / "minimal-contract.md"), parse_mode="expansion"
    )

    spec = service.compile_plan_to_spec(plan_doc, parent)

    # Per-phase sandwich: phase-p1 = TEST + 1.1::impl + REF; phase-p2 same shape.
    assert len(spec["tasks"]) == 6
    assert {phase["id"]: len(phase["task_ids"]) for phase in spec["phases"]} == {
        "phase-p1": 3,
        "phase-p2": 3,
    }
    # Cross-phase chain: phase N+1's [TEST] depends on phase N's [REF].
    assert {"task_id": "phase-p2::__test", "depends_on": "phase-p1::__ref"} in spec[
        "dependencies"
    ]
    # Cross-deliverable manifest depends_on: 2.1 → 1.1 wires IMPL → IMPL.
    assert {"task_id": "2.1::impl", "depends_on": "1.1::impl"} in spec["dependencies"]
    assert spec["deferrals"] == [
        {
            "section_id": "2.2",
            "task_ref": "#99",
            "reason": "covered by downstream follow-up",
            "owner": "docs",
            "original_acceptance_items": [
                {
                    "item_id": "2.2.1",
                    "artifact_kind": "behavior",
                    "artifact_ref": "downstream lifecycle",
                }
            ],
        }
    ]
    assert all(
        "covers:minimal-contract:2.2" not in label
        for task in spec["tasks"]
        for label in task["labels"]
    )


def test_apply_contract_spec_persists_covers_labels_without_extra_phase_wrappers(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Minimal epic",
        task_type="epic",
    )
    plan_doc = parse_plan(
        _write_minimal_plan(tmp_path / "minimal-contract.md"), parse_mode="expansion"
    )
    spec = service.compile_plan_to_spec(plan_doc, parent)
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
        plan_file=str(plan_doc.source_path),
    )
    service.run_manager.save_compiled_spec(run.id, spec)

    applied = service.apply_run(run.id, session_id=None)

    assert len(applied.created_task_ids) == 6
    created = [service.task_manager.get_task(task_id) for task_id in applied.created_task_ids]
    assert all(task is not None for task in created)
    titles = [task.title for task in created if task is not None]
    # Exactly one phase-level [TEST] Phase N and [REF] Phase N per phase —
    # the contract compile emits the sandwich, the apply step must NOT
    # double-wrap (tdd_sandwich_emitted=True suppresses the apply-side
    # wrapper). Two phases → two TEST + two REF.
    assert sum(1 for title in titles if title.startswith("[TEST] Phase")) == 2
    assert sum(1 for title in titles if title.startswith("[REF] Phase")) == 2
    assert any(
        "covers:minimal-contract:1.1:1.1.1" in (task.labels or [])
        for task in created
        if task is not None
    )
