"""Expansion compile dispatch between contract parser and ad-hoc LLM expansion."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def run_manager(temp_db) -> LocalExpansionRunManager:
    return LocalExpansionRunManager(temp_db)


@pytest.fixture
def service(
    task_manager: LocalTaskManager,
    run_manager: LocalExpansionRunManager,
) -> ExpansionService:
    return ExpansionService(
        task_manager=task_manager,
        llm_service=MagicMock(),
        run_manager=run_manager,
    )


def _parent(service: ExpansionService, sample_project: dict[str, Any]) -> Task:
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )


def _valid_spec(parent_id: str, plan_file: str | None) -> dict[str, Any]:
    return {
        "version": 1,
        "parent_task_id": parent_id,
        "plan_file": plan_file,
        "phases": [{"id": "phase-1", "title": "Phase", "summary": "", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Leaf",
                "description": "Do the work.",
                "category": "code",
                "validation": "Work is complete.",
            }
        ],
        "dependencies": [],
        "execution_groups": [],
    }


@pytest.mark.asyncio
async def test_contract_plan_dispatches_to_deterministic_compile(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan = tmp_path / "contract-plan.md"
    plan.write_text(
        """> **Plan ID:** test-contract-plan

# Test Contract Plan

## P1 Phase
`kind: framing`

### 1.1 Build Leaf [category: code]
`kind: deliverable`

Implement the behavior.

**Acceptance:**
- 1.1.1 - Behavior exists. file: `src/example.py`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Build Leaf
  category: code
  task_type: task
  depends_on: []
  validation_criteria: Behavior exists.
  labels:
    - covers:test-contract-plan:1.1:1.1.1
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.1"
```
""",
        encoding="utf-8",
    )
    plan_file = str(plan)
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
        plan_file=plan_file,
    )

    raw_spec = AsyncMock()
    with (
        patch.object(service, "_generate_raw_spec", raw_spec),
        patch.object(
            service,
            "compile_plan_to_spec",
            return_value=_valid_spec(parent.id, plan_file),
        ) as deterministic_compile,
    ):
        refreshed = await service.compile_run(run.id)

    assert refreshed.compiled_spec is not None
    assert deterministic_compile.call_count == 1
    assert raw_spec.await_count == 0


@pytest.mark.asyncio
async def test_non_contract_plan_file_is_rejected_without_llm_fallback(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan = tmp_path / "freeform.md"
    plan.write_text(
        """# Expansion Notes

## Phase 1: Build the thing

- Add the implementation.
- Add tests.
""",
        encoding="utf-8",
    )
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
        plan_file=str(plan),
    )

    raw_spec = AsyncMock(return_value=_valid_spec(parent.id, str(plan)))
    with (
        patch.object(service, "_generate_raw_spec", raw_spec),
        patch.object(service, "compile_plan_to_spec") as deterministic_compile,
    ):
        with pytest.raises(ValueError, match=r"(Plan file must conform|no kind: deliverable)"):
            await service.compile_run(run.id)

    assert deterministic_compile.call_count == 0
    assert raw_spec.await_count == 0


@pytest.mark.asyncio
async def test_missing_plan_file_is_rejected_without_llm_fallback(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan_file = tmp_path / "missing.md"
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
        plan_file=str(plan_file),
    )

    raw_spec = AsyncMock(return_value=_valid_spec(parent.id, str(plan_file)))
    with (
        patch.object(service, "_generate_raw_spec", raw_spec),
        patch.object(service, "compile_plan_to_spec") as deterministic_compile,
    ):
        with pytest.raises(ValueError, match="Plan file must conform"):
            await service.compile_run(run.id)

    assert deterministic_compile.call_count == 0
    assert raw_spec.await_count == 0


@pytest.mark.asyncio
async def test_no_plan_file_uses_ad_hoc_llm_path(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )

    raw_spec = AsyncMock(return_value=_valid_spec(parent.id, None))
    with (
        patch.object(service, "_generate_raw_spec", raw_spec),
        patch.object(service, "compile_plan_to_spec") as deterministic_compile,
    ):
        refreshed = await service.compile_run(run.id)

    assert refreshed.compiled_spec is not None
    assert deterministic_compile.call_count == 0
    assert raw_spec.await_count == 1
