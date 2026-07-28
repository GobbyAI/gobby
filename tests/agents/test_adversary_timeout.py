from __future__ import annotations

import json
from pathlib import Path

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_terminal import terminalize_plan_review_run
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.test_terminal_paths import _bound_review


def test_timeout_classification_retention_and_wake_isolation(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    bound = _bound_review(temp_db, tmp_path, suffix="-timeout")
    manager = LocalAgentRunManager(temp_db)

    outcome = terminalize_plan_review_run(
        manager,
        run_id=bound.run_id,
        action="timeout",
        error="Agent exceeded review timeout",
        timeout_seconds=45,
        tool_calls_count=7,
        turns_used=3,
    )

    assert outcome.handled is True
    assert outcome.parent_session_id == bound.parent_session_id
    assert outcome.run is not None
    assert outcome.run.status == "timeout"
    assert outcome.run.result is not None
    result = json.loads(outcome.run.result)
    assert result["verdict"] == "inconclusive"
    assert result["reason"] == {
        "reason_code": "timeout",
        "timeout_seconds": 45,
    }
    assert result["convergence_telemetry"]["state"] == "enriched"
    assert result["convergence_telemetry"]["reviewer"] == {
        "status": "unavailable",
        "reason": "reviewer_result_not_delivered",
    }
    assert result["convergence_telemetry"]["daemon"]["terminal_status"] == "timeout"
    assert PlanReviewEvidenceService(temp_db).get_evidence(bound.evidence_id).expired_at is not None
