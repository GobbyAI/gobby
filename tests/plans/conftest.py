from __future__ import annotations

from pathlib import Path

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project


@pytest.fixture
def review_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PlanReviewEvidenceService, str, str, Path]:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path, name="review-evidence", monkeypatch=monkeypatch
    )
    session = SessionManager(temp_db).register(
        external_id="review-evidence-parent",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=isolated.project.id,
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
    return PlanReviewEvidenceService(temp_db), isolated.project.id, session.id, plan_path
