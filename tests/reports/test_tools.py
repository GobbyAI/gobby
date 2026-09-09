"""Public evidence readers preserve paging and publication/source separation."""

from datetime import UTC, datetime

import pytest

from gobby.feedback.storage import FeedbackReviewStore, FeedbackRow
from gobby.mcp_proxy.tools.feedback import create_feedback_registry
from gobby.mcp_proxy.tools.reports import create_reports_registry
from gobby.memory.dream.decisions import DreamDecisionStore
from gobby.memory.dream.models import DreamAction
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from tests.reports.test_publication import _content, _source

pytestmark = pytest.mark.integration


async def test_feedback_readers_page_frozen_observations_and_actual_outcomes(
    temp_db: HubDatabase,
) -> None:
    store = FeedbackReviewStore(temp_db)
    observations = [
        FeedbackRow(
            id=str(index),
            session_id="session",
            source="user",
            kind="friction",
            kind_other_label=None,
            evidence=f"evidence {index}",
            impact="medium",
            frequency="once",
            suggestion=None,
            disposition=None,
            created_at=datetime.now(UTC),
        )
        for index in range(3)
    ]
    run_id = store.create_run(
        dry_run=False,
        window_start=None,
        window_end=None,
        rows_considered=3,
        observations=observations,
    )
    store.finalize_run(
        run_id,
        status="partial",
        findings={"clusters": [{"observation_ids": [str(i)]} for i in range(3)]},
        actions={
            "filed": [{"observation_ids": ["0"], "task_ref": "#42"}],
            "failed": [{"observation_ids": ["1"], "error": "task storage offline"}],
        },
    )
    registry = create_feedback_registry(temp_db)
    first = await registry.call("get_review_observations", {"run_id": run_id, "limit": 2})
    assert [row["id"] for row in first["observations"]] == ["0", "1"]
    last = await registry.call(
        "get_review_observations",
        {
            "run_id": run_id,
            "offset": first["next_offset"],
            "limit": 2,
        },
    )
    assert [row["id"] for row in last["observations"]] == ["2"]
    assert last["next_offset"] is None
    result = await registry.call("get_review_results", {"run_id": run_id, "offset": 1, "limit": 1})
    assert result["status"] == "partial"
    assert result["outcomes"]["filed"] == []
    assert result["outcomes"]["failed"][0]["error"] == "task storage offline"


async def test_dream_evidence_distinguishes_historical_from_exhausted_ledger(
    temp_db: HubDatabase,
) -> None:
    registry = create_reports_registry(temp_db, project_id=PERSONAL_PROJECT_ID)
    run_id = _source(temp_db, "dream")
    historical = await registry.call(
        "get_report_evidence",
        {
            "source_kind": "dream",
            "run_id": run_id,
            "offset": 100,
        },
    )
    assert historical["historical_rationale_missing"] is True
    assert historical["snapshots"] == []
    ledger = DreamDecisionStore(temp_db)
    decision = ledger.stage(run_id, [DreamAction(action="keep", reason="retain")], [])[0]
    ledger.finish(decision, "noop", mutations=0)
    exhausted = await registry.call(
        "get_report_evidence",
        {
            "source_kind": "dream",
            "run_id": run_id,
            "offset": 100,
        },
    )
    assert exhausted["historical_rationale_missing"] is False
    assert exhausted["decisions"] == [] and exhausted["next_offset"] is None
    assert "snapshots" not in exhausted


async def test_report_tools_retain_draft_and_failure_on_explicit_retry(
    temp_db: HubDatabase,
) -> None:
    registry = create_reports_registry(temp_db, project_id=PERSONAL_PROJECT_ID)
    run_id = _source(temp_db, "feedback", "partial")
    args = {"source_kind": "feedback", "run_id": run_id}
    await registry.call("request_report", args)
    from gobby.reports.storage import ReportStore

    store = ReportStore(temp_db)
    store.begin("feedback", run_id)
    content = _content(run_id)
    await registry.call("save_report_draft", {**args, "content": content})
    await registry.call(
        "record_report_failure", {**args, "phase": "publication", "error": "commit rejected"}
    )
    report = await registry.call("retry_report", args)
    assert report["status"] == "pending"
    draft = await registry.call("get_report", args)
    assert draft["content"] == content
    history = await registry.call("get_report_attempts", args)
    assert len(history["attempts"]) == 1
    assert history["attempts"][0]["error"] == "commit rejected"
    source = FeedbackReviewStore(temp_db).get_run(run_id)
    assert source is not None and source.status == "partial"
