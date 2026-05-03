"""Manifest-driven contract plan compile tests."""

from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.plans.parser import PlanDocument, parse_plan
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(
        task_manager=LocalTaskManager(temp_db),
        llm_service=MagicMock(),
        run_manager=LocalExpansionRunManager(temp_db),
    )


def _parent(service: ExpansionService, sample_project: dict[str, Any]) -> Task:
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Manifest-driven epic",
        task_type="epic",
    )


def _write_plan(tmp_path: Path, text: str, name: str = "manifest-driven.md") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip("\n").rstrip() + "\n", encoding="utf-8")
    return path


def _parse_manifest_plan(tmp_path: Path) -> PlanDocument:
    return parse_plan(_write_plan(tmp_path, _MANIFEST_PLAN), parse_mode="expansion")


def _deps_for(spec: dict[str, Any], task_id: str) -> set[str]:
    return {edge["depends_on"] for edge in spec["dependencies"] if edge["task_id"] == task_id}


_MANIFEST_PLAN = """
> **Plan ID:** manifest-driven

## P1 Foundation
`kind: framing`

### 1.1 Bootstrap Section
`kind: deliverable`

Bootstrap body copied into the generated task description.

**Acceptance:**
- 1.1.1 - Bootstrap exists. file: `src/bootstrap.py`

### 1.2 Core Section
`kind: deliverable`

Core body copied into the generated task description.

**Acceptance:**
- 1.2.1 - Core exists. file: `src/core.py`

### 1.3 Audit Section
`kind: deliverable`

Audit body copied into the generated task description.

**Acceptance:**
- 1.3.1 - Audit exists. file: `tests/test_audit.py`

## P2 Integration
`kind: framing`

### 2.1 Docs Section
`kind: deliverable`

Docs body copied into the generated task description.

**Acceptance:**
- 2.1.1 - Docs exist. file: `docs/core.md`

### 2.2 Release Section
`kind: deliverable`

Release body copied into the generated task description.

**Acceptance:**
- 2.2.1 - Release exists. file: `docs/release.md`

## P3 Finalize
`kind: framing`

### 3.1 Final Section
`kind: deliverable`

Final body copied into the generated task description.

**Acceptance:**
- 3.1.1 - Final tests exist. test: `tests/test_final.py`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "Bootstrap from manifest"
  category: manual
  task_type: chore
  depends_on: []
  validation_criteria: "Bootstrap validation from manifest"
  labels:
    - "covers:manifest-driven:1.1:1.1.1"
    - "manifest:bootstrap"
  assigned_agent: backend-developer
  tdd: false
  source_section: "1.1"
- title: "Core from manifest"
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: "Core validation from manifest"
  labels:
    - "covers:manifest-driven:1.2:1.2.1"
    - "manifest:core"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.2"
- title: "Audit from manifest"
  category: test
  task_type: task
  depends_on:
    - "1.2"
  validation_criteria: "Audit validation from manifest"
  labels:
    - "covers:manifest-driven:1.3:1.3.1"
    - "manifest:audit"
  assigned_agent: backend-developer
  tdd: false
  source_section: "1.3"
- title: "Docs from manifest"
  category: docs
  task_type: task
  depends_on:
    - "1.3"
  validation_criteria: "Docs validation from manifest"
  labels:
    - "covers:manifest-driven:2.1:2.1.1"
    - "manifest:docs"
  assigned_agent: backend-developer
  tdd: false
  source_section: "2.1"
- title: "Release from manifest"
  category: docs
  task_type: task
  depends_on:
    - "2.1"
  validation_criteria: "Release validation from manifest"
  labels:
    - "covers:manifest-driven:2.2:2.2.1"
    - "manifest:release"
  assigned_agent: backend-developer
  tdd: false
  source_section: "2.2"
- title: "Final from manifest"
  category: code
  task_type: task
  depends_on:
    - "2.2"
  validation_criteria: "Final validation from manifest"
  labels:
    - "covers:manifest-driven:3.1:3.1.1"
    - "manifest:final"
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.1"
```
"""


def test_manifest_entry_source_section_must_resolve_to_deliverable(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _parse_manifest_plan(tmp_path)
    bad_entry = replace(plan_doc.manifest_entries[0], source_section="P1")
    malformed_doc = replace(
        plan_doc,
        manifest_entries=(bad_entry, *plan_doc.manifest_entries[1:]),
    )

    with pytest.raises(ValueError, match="source_section='P1'.*kind: deliverable"):
        service.compile_plan_to_spec(malformed_doc, parent)


def test_non_tdd_manifest_entry_uses_single_task_id(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_parse_manifest_plan(tmp_path), parent)

    assert any(task["task_id"] == "2.1::single" for task in spec["tasks"])
    assert any(task["task_id"] == "1.3::single" for task in spec["tasks"])
    assert not any(task["task_id"] == "1.3::impl" for task in spec["tasks"])


def test_entry_fields_preserved(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_parse_manifest_plan(tmp_path), parent)

    bootstrap_tasks = [task for task in spec["tasks"] if task["source_section_id"] == "1.1"]
    assert len(bootstrap_tasks) == 1
    bootstrap = bootstrap_tasks[0]
    assert bootstrap["id"] == "1.1::single"
    assert bootstrap["phase_id"] == "phase-p1"
    assert bootstrap["title"] == "Bootstrap from manifest"
    assert "Bootstrap body copied into the generated task description." in bootstrap["description"]
    assert bootstrap["priority"] == 2
    assert bootstrap["task_type"] == "chore"
    assert bootstrap["category"] == "manual"
    assert bootstrap["validation"] == (
        "Bootstrap validation from manifest\n"
        "Acceptance artifacts:\n"
        "- 1.1.1: file: `src/bootstrap.py`"
    )
    assert bootstrap["affected_files"] == ["src/bootstrap.py"]
    assert bootstrap["labels"] == ["covers:manifest-driven:1.1:1.1.1", "manifest:bootstrap"]
    assert bootstrap["assigned_agent"] == "backend-developer"
    assert bootstrap["additional_skills"] == []
    assert "validation_criteria" not in bootstrap

    core_tasks = [task for task in spec["tasks"] if task["source_section_id"] == "1.2"]
    assert [task["title"] for task in core_tasks] == ["[IMPL] Core from manifest"]
    assert [task["category"] for task in core_tasks] == ["code"]
    assert {task["validation"] for task in core_tasks} == {
        "Core validation from manifest\nAcceptance artifacts:\n- 1.2.1: file: `src/core.py`"
    }
    assert {tuple(task["labels"]) for task in core_tasks} == {
        ("covers:manifest-driven:1.2:1.2.1", "manifest:core")
    }
    assert {task["task_type"] for task in core_tasks} == {"feature"}


def test_cross_tdd_mode_dependencies(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_parse_manifest_plan(tmp_path), parent)

    # Cross-deliverable depends_on edges link IMPL/single tasks directly,
    # regardless of TDD status — the per-phase sandwich wraps but does not
    # rewire cross-deliverable dependencies.
    assert "1.1::single" in _deps_for(spec, "1.2::impl")
    assert "1.2::impl" in _deps_for(spec, "1.3::single")
    assert "1.3::single" in _deps_for(spec, "2.1::single")
    assert "2.1::single" in _deps_for(spec, "2.2::single")
    assert "2.2::single" in _deps_for(spec, "3.1::impl")

    # Within-phase TDD sandwich: every [IMPL] depends on its phase [TEST];
    # phase [REF] depends on every [IMPL] in that phase.
    assert "phase-p1::__test" in _deps_for(spec, "1.2::impl")
    assert {"1.2::impl"} <= _deps_for(spec, "phase-p1::__ref")
    assert "phase-p3::__test" in _deps_for(spec, "3.1::impl")
    assert "3.1::impl" in _deps_for(spec, "phase-p3::__ref")

    # Cross-phase chain skips phase-p2 (no TDD entries → no sandwich).
    # phase-p3's [TEST] therefore depends on phase-p1's [REF], not p2.
    assert "phase-p1::__ref" in _deps_for(spec, "phase-p3::__test")


def test_phase_nesting_p1_p2_p3(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_parse_manifest_plan(tmp_path), parent)

    # phase-p1: TDD entry [1.2] wrapped in sandwich; non-TDD 1.1 and 1.3
    # singles follow the sandwich.
    # phase-p2: no TDD entries → no sandwich, just two singles.
    # phase-p3: TDD entry 3.1 wrapped in sandwich.
    assert {phase["id"]: phase["task_ids"] for phase in spec["phases"]} == {
        "phase-p1": [
            "phase-p1::__test",
            "1.2::impl",
            "phase-p1::__ref",
            "1.1::single",
            "1.3::single",
        ],
        "phase-p2": ["2.1::single", "2.2::single"],
        "phase-p3": ["phase-p3::__test", "3.1::impl", "phase-p3::__ref"],
    }
    assert spec["deliverable_count"] == 6
    # 6 entries (1 task each) + sandwich wrappers in p1 (TEST+REF) + p3 (TEST+REF) = 10
    assert len(spec["tasks"]) == 10
    # phase-p2 has no TDD entries → no sandwich emitted.
    phase_by_id = {phase["id"]: phase for phase in spec["phases"]}
    assert phase_by_id["phase-p1"]["tdd_sandwich_emitted"] is True
    assert phase_by_id["phase-p2"]["tdd_sandwich_emitted"] is False
    assert phase_by_id["phase-p3"]["tdd_sandwich_emitted"] is True


def test_missing_manifest_raises(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = _write_plan(
        tmp_path,
        """
        > **Plan ID:** missing-manifest

        ## A1 Missing Manifest
        `kind: deliverable`

        **Acceptance:**
        - A1.1 - Missing manifest is rejected. file: `src/missing.py`
        """,
        name="missing-manifest.md",
    )
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
        plan_file=str(plan_path),
    )

    with pytest.raises(ValueError, match="Plan file must conform to the Plan-Coverage Contract"):
        service._parse_contract_plan(run, parent)


def test_deferrals_preserved(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = _write_plan(
        tmp_path,
        """
        > **Plan ID:** deferral-manifest

        ## A1 Immediate
        `kind: deliverable`

        **Acceptance:**
        - A1.1 - Immediate work exists. file: `src/immediate.py`

        ## D1 Deferred
        `kind: deferred`

        ```yaml
        task_ref: "#777"
        reason: "tracked downstream"
        owner: "backend-developer"
        original_acceptance_items:
          - item_id: D1.1
            prose: "Deferred work is tracked. behavior: downstream work"
            artifact_kind: behavior
            artifact_ref: "downstream work"
        ```

        ## M1 Task Manifest
        `kind: manifest`

        ```yaml
        - title: "Immediate from manifest"
          category: code
          task_type: task
          depends_on: []
          validation_criteria: "Immediate validation from manifest"
          labels:
            - "covers:deferral-manifest:A1:A1.1"
          assigned_agent: backend-developer
          tdd: false
          source_section: "A1"
        ```
        """,
        name="deferral-manifest.md",
    )

    spec = service.compile_plan_to_spec(parse_plan(plan_path, parse_mode="expansion"), parent)

    assert len(spec["tasks"]) == 1
    assert spec["tasks"][0]["id"] == "A1::single"
    assert spec["deferrals"] == [
        {
            "section_id": "D1",
            "task_ref": "#777",
            "reason": "tracked downstream",
            "owner": "backend-developer",
            "original_acceptance_items": [
                {
                    "item_id": "D1.1",
                    "artifact_kind": "behavior",
                    "artifact_ref": "downstream work",
                }
            ],
        }
    ]
