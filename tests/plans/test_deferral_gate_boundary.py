"""Expansion validation leaves deferral ownership to the coverage gate.

The contract names expansion coverage QA as the first deferral gate: expansion-mode
validation runs before the root epic is registered, so it cannot know the recovery
epic whose dependency closure a deferral must join. These tests pin both halves of
that boundary on one synthetic plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.plans.deferral import validate_deferral
from gobby.plans.manifest_emitter import emit_stub_manifest
from gobby.plans.parser import parse_plan
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit

PLAN_ID = "deferral-gate-boundary"
EPIC_REF = "#100"
TARGET_REF = "#200"
PARENT_REF = "#300"


def _write_plan(path: Path) -> Path:
    path.write_text(
        f"""> **Plan ID:** {PLAN_ID}

# Deferral Gate Boundary

## P1: Setup
`kind: framing`

### 1.1 Foundation [category: code]
`kind: deliverable`

Target: `src/foundation.py`

Implement the first behavior.

**Acceptance:**
- 1.1.1 - Foundation exists. file: `src/foundation.py`

## D1 Follow-up Work
`kind: deferred`

```yaml
deferral:
  task_ref: "{TARGET_REF}"
  reason: "Owned by another epic."
  owner: "backend"
  original_acceptance_items:
    - D1.1
```
""",
        encoding="utf-8",
    )
    assert emit_stub_manifest(path, plan_id=PLAN_ID) == "fresh"
    return path


class _Store:
    """Live task tree after registration: the target has no route into the epic."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {
            EPIC_REF: {"state": "open"},
            TARGET_REF: {"state": "open", "validation_criteria": "Covers D1.1."},
            PARENT_REF: {"state": "open"},
        }
        self.labels: dict[str, list[str]] = {
            TARGET_REF: [f"deferred-from:{PLAN_ID}:D1"],
            PARENT_REF: [],
        }
        self.dependencies: dict[str, list[str]] = {EPIC_REF: []}

    def get_task(self, task_ref: str) -> dict[str, Any] | None:
        return self.tasks.get(task_ref)

    def get_task_labels(self, task_ref: str) -> list[str]:
        return list(self.labels.get(task_ref, []))

    def get_task_dependencies(self, task_ref: str) -> list[str]:
        return list(self.dependencies.get(task_ref, []))

    def find_task_ref_by_label(self, label: str) -> str | None:
        return None


def _coverage_gate(plan_path: Path, store: _Store) -> str:
    document = parse_plan(plan_path, parse_mode="expansion")
    section = next(s for s in document.sections if s.deferral is not None)
    assert section.deferral is not None
    return validate_deferral(
        section.deferral, PLAN_ID, section.section_id, store, recovery_epic_ref=EPIC_REF
    ).status


def test_expansion_validation_accepts_a_deferral_the_coverage_gate_rejects(
    temp_db: Any, tmp_path: Path, sample_project: dict[str, Any]
) -> None:
    plan_path = _write_plan(tmp_path / "plan.md")
    tasks = LocalTaskManager(temp_db)
    service = ExpansionService(task_manager=tasks, llm_service=MagicMock())

    result = service.validate_plan_file(
        plan_path,
        project_context={"id": sample_project["id"]},
        task_manager=tasks,
        parse_mode="expansion",
    )

    assert result["valid"] is True, result["errors"]
    assert _coverage_gate(plan_path, _Store()) == "missing_dependency_or_cited_parent"


def test_coverage_gate_accepts_both_documented_deferral_routes(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path / "plan.md")

    dependency = _Store()
    dependency.dependencies[EPIC_REF] = [TARGET_REF]
    assert _coverage_gate(plan_path, dependency) == "valid"

    cited = _Store()
    cited.labels[TARGET_REF].append(f"cited-parent:{PARENT_REF}")
    cited.labels[PARENT_REF].append(f"out-of-scope-for:{EPIC_REF}")
    assert _coverage_gate(plan_path, cited) == "valid"
