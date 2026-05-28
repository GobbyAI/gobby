"""Tests for `ExpansionService.validate_plan_file` semantic gates.

Specifically: deliverables-with-no-phases must fail validation. The parser's
`draft` mode silently drops headings whose IDs do not match the canonical
section regex, so a plan authored with `## Phase 1: Setup` (literal word
"Phase") parses cleanly but yields zero phase sections — which the expansion
compiler cannot turn into a phase hierarchy. The validator must surface this as
``valid: False`` instead of letting the compiler choke later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db: Any) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def _write_plan_with_old_phase_form(path: Path) -> Path:
    """Plan whose phase headings use the pre-contract `## Phase N: Name` form.

    The parser silently drops these because "Phase" prevents the section_id
    regex from matching, so the result has deliverables but zero phases.
    """
    path.write_text(
        """> **Plan ID:** broken-phase-form

# Broken Phase Form

## Phase 1: Setup
`kind: framing`

### 1.1 Foundation [category: code]
`kind: deliverable`

Target: `src/foundation.py`

Implement the first behavior.

**Acceptance:**
- 1.1.1 - Foundation exists. file: `src/foundation.py`
""",
        encoding="utf-8",
    )
    return path


def _write_plan_with_canonical_phase_form(path: Path) -> Path:
    """Plan whose phase heading matches the contract regex `^P\\d+$`."""
    path.write_text(
        """> **Plan ID:** canonical-phase-form

# Canonical Phase Form

## P1: Setup
`kind: framing`

### 1.1 Foundation [category: code]
`kind: deliverable`

Target: `src/foundation.py`

Implement the first behavior.

**Acceptance:**
- 1.1.1 - Foundation exists. file: `src/foundation.py`
""",
        encoding="utf-8",
    )
    return path


def _write_plan_with_manual_manifest_category(path: Path) -> Path:
    """Write a plan whose manifest uses the unsupported manual leaf category.

    Args:
        path: Destination Markdown file.

    Returns:
        The written plan path.
    """
    path.write_text(
        """> **Plan ID:** manual-manifest

# Manual Manifest

## P1: Verification
`kind: framing`

### 1.1 Run live verification [category: manual]
`kind: deliverable`

Target: `tests/live.md`

Verify the live behavior.

**Acceptance:**
- 1.1.1 - Live verification passes. behavior: "live verification passes" in `tests/live.md`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "Run live verification"
  category: manual
  task_type: task
  depends_on: []
  validation_criteria: '"live verification passes" in `tests/live.md`'
  labels:
    - "covers:manual-manifest:1.1:1.1.1"
  assigned_agent: backend-developer
  tdd: false
  source_section: "1.1"
```
""",
        encoding="utf-8",
    )
    return path


def _write_plan_with_unknown_manifest_task_type(path: Path) -> Path:
    """Write a plan whose manifest uses an unsupported task_type value."""
    path.write_text(
        """> **Plan ID:** unknown-task-type-manifest

# Unknown Task Type Manifest

## P1: Documentation
`kind: framing`

### 1.1 Document behavior [category: docs]
`kind: deliverable`

Target: `docs/behavior.md`

Document the behavior.

**Acceptance:**
- 1.1.1 - Behavior is documented. file: `docs/behavior.md`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "Document behavior"
  category: docs
  task_type: guide
  depends_on: []
  validation_criteria: "`docs/behavior.md` exists"
  labels:
    - "covers:unknown-task-type-manifest:1.1:1.1.1"
  assigned_agent: backend-developer
  tdd: false
  source_section: "1.1"
```
""",
        encoding="utf-8",
    )
    return path


def test_validate_plan_file_rejects_deliverables_without_phases(
    service: ExpansionService, tmp_path: Path
) -> None:
    """Reject non-canonical phase headings before expansion compile."""
    plan_path = _write_plan_with_old_phase_form(tmp_path / "broken.md")

    result = service.validate_plan_file(plan_path)

    assert result["valid"] is False
    assert "errors" in result
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert "no phase sections" in error
    assert "^P\\d+$" in error
    assert "## P1: Setup" in error


def test_validate_plan_file_accepts_canonical_phase_form(
    service: ExpansionService, tmp_path: Path
) -> None:
    """Accept canonical P-numbered phase headings."""
    plan_path = _write_plan_with_canonical_phase_form(tmp_path / "canonical.md")

    result = service.validate_plan_file(plan_path)

    assert result["valid"] is True
    assert result["phase_count"] >= 1
    assert result["deliverable_count"] >= 1
    assert 1 in result["phases"]


def test_validate_plan_file_returns_semantic_lint_errors(
    service: ExpansionService, tmp_path: Path
) -> None:
    """Return semantic lint failures in validate_plan_file output."""
    plan_path = _write_plan_with_canonical_phase_form(tmp_path / "canonical.md")
    text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(text.replace("Target: `src/foundation.py`\n\n", ""), encoding="utf-8")

    result = service.validate_plan_file(plan_path)

    assert result["valid"] is False
    assert any("target-coverage" in error for error in result["errors"])
    assert result["semantic_lint"]["valid"] is False


def test_validate_plan_file_rejects_manual_manifest_category(
    service: ExpansionService, tmp_path: Path
) -> None:
    """Reject manual manifest categories for generated task leaves."""
    plan_path = _write_plan_with_manual_manifest_category(tmp_path / "manual.md")

    result = service.validate_plan_file(plan_path)

    assert result["valid"] is False
    assert any("unsupported category 'manual'" in error for error in result["errors"])
    assert any("development-forward categories" in error for error in result["errors"])


def test_validate_plan_file_rejects_unknown_manifest_task_type(
    service: ExpansionService, tmp_path: Path
) -> None:
    """Reject manifest task_type values that are neither canonical nor aliases."""
    plan_path = _write_plan_with_unknown_manifest_task_type(tmp_path / "unknown-task-type.md")

    result = service.validate_plan_file(plan_path)

    assert result["valid"] is False
    assert any("Invalid task_type 'guide'" in error for error in result["errors"])
