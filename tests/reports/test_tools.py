"""Source evidence remains available after synthesis publication retirement."""

from datetime import UTC, datetime

import pytest

from gobby.feedback.storage import FeedbackReviewStore, FeedbackRow
from gobby.mcp_proxy.tools.feedback import create_feedback_registry
from gobby.memory.dream.decisions import DreamDecisionStore
from gobby.memory.dream.models import DreamAction
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID

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
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    ledger = DreamDecisionStore(temp_db)
    historical = ledger.page(run_id, offset=100)
    assert historical["historical_rationale_missing"] is True
    decision = ledger.stage(run_id, [DreamAction(action="keep", reason="retain")], [])[0]
    ledger.finish(decision, "noop", mutations=0)
    exhausted = ledger.page(run_id, offset=100)
    assert exhausted["historical_rationale_missing"] is False
    assert exhausted["decisions"] == [] and exhausted["next_offset"] is None
    assert "snapshots" not in exhausted
