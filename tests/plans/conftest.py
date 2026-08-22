from __future__ import annotations

from pathlib import Path

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id


@pytest.fixture
def review_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> tuple[PlanReviewEvidenceService, str, str, Path]:
    project = LocalProjectManager(temp_db).create(
        name="review-evidence",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="review-evidence-parent",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project.id,
    )
    plan_dir = tmp_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "review-evidence.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Review Evidence",
                "**Plan ID:** review-evidence",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Work",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds yet.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "- title: Implement example",
                "  source_section: '1.1'",
                "  covers:",
                "    - 1.1.1",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels:",
                "    - covers:review-evidence:1.1:1.1.1",
                "  description: Implement the example.",
                "  validation_criteria: Example behavior is tested.",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return PlanReviewEvidenceService(temp_db), project.id, session.id, plan_path
