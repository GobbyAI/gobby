from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.storage.tasks._stage_test_helpers import set_stage_state


@dataclass(frozen=True)
class StageReviewSetup:
    db: HubDatabase
    manager: LocalTaskManager
    evidence: PlanReviewEvidenceService
    runs: LocalAgentRunManager
    sessions: SessionManager
    project_id: str
    task_id: str
    plan_path: Path
    plan_relative_path: str
    parent_session_id: str


@pytest.fixture(name="stage_review_setup")
def stage_review_setup(temp_db: HubDatabase, tmp_path: Path) -> StageReviewSetup:
    project = LocalProjectManager(temp_db).create(
        name="stage-review-findings",
        repo_path=str(tmp_path),
    )
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="stage-review-launcher",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / ".gobby" / "plans" / "review.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "\n".join(
            [
                "# Review",
                "**Plan ID:** review",
                "",
                "## P1 Foundation",
                "`kind: framing`",
                "",
                "### 1.1 Implement",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Implemented. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "- title: Implement",
                "  source_section: '1.1'",
                "  covers: [1.1.1]",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels: [covers:review:1.1:1.1.1]",
                "  description: Implement.",
                "  validation_criteria: Tested.",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project.id,
        "Plan review anchor",
        task_type="task",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(task.id, stage_names=["planning"])
    set_stage_state(temp_db, task.id, "planning", "needs_review")
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        plan_file_path=str(plan_path),
    )
    return StageReviewSetup(
        db=temp_db,
        manager=manager,
        evidence=PlanReviewEvidenceService(temp_db),
        runs=LocalAgentRunManager(temp_db),
        sessions=sessions,
        project_id=project.id,
        task_id=task.id,
        plan_path=plan_path,
        plan_relative_path=".gobby/plans/review.md",
        parent_session_id=parent.id,
    )


def _hold_dispatch_mutex(
    setup: StageReviewSetup,
    *,
    task_id: str,
    run_id: str,
) -> None:
    mutexes = TaskDispatchMutexManager(setup.db)
    assert mutexes.acquire_mutex(
        task_id,
        holder=f"test:{run_id}",
        kind="spawn_agent",
        ttl_seconds=600,
        run_id=run_id,
    )


def _prepare_bound(
    setup: StageReviewSetup,
    *,
    round_number: int = 1,
    task_id: str | None = None,
    plan_path: Path | None = None,
) -> tuple[str, str]:
    target_task_id = task_id or setup.task_id
    prepared = setup.evidence.prepare_plan_review_round(
        project_id=setup.project_id,
        plan_path=plan_path or setup.plan_path,
        round_number=round_number,
        task_id=target_task_id,
        stage="planning",
    )
    run = setup.runs.create(
        parent_session_id=setup.parent_session_id,
        provider="codex",
        prompt="review",
        task_id=target_task_id,
    )
    setup.evidence.bind_evidence_run(prepared.evidence_id, run.id)
    _hold_dispatch_mutex(setup, task_id=target_task_id, run_id=run.id)
    return prepared.evidence_id, run.id
