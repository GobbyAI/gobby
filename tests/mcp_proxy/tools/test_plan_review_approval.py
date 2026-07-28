from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._plan_review_approval import (
    _checkpoint_failure,
    complete_plan_review_mint,
)
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError

pytestmark = pytest.mark.unit


def test_nonreplay_mint_skips_evidence_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mint(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"lesson_mint_status": "minted"}

    def forbid_evidence_service(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("non-replay mint fetched plan-review evidence")

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._plan_review_approval.mint_plan_review_lessons",
        mint,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._plan_review_approval.PlanReviewEvidenceService",
        forbid_evidence_service,
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(db=object()),
            review_learning_service=object(),
        ),
    )

    result = complete_plan_review_mint(
        ctx,
        task_id="task-id",
        stage="planning",
        evidence_id="evidence-id",
        session_id="session-id",
        replay=False,
    )

    assert result == {"lesson_mint_status": "minted"}


@pytest.mark.parametrize(
    "error",
    [
        ReviewEvidenceError("checkpoint_failed", "review evidence failed"),
        psycopg.Error("database failed"),
    ],
)
def test_checkpoint_failure_degrades_when_checkpoint_write_fails(
    error: Exception,
) -> None:
    service = MagicMock(spec=PlanReviewEvidenceService)
    service.checkpoint_plan_review_lesson_mint.side_effect = error

    result = _checkpoint_failure(
        cast(PlanReviewEvidenceService, service),
        "evidence-id",
        "mint failed",
    )

    assert result["lesson_mint_status"] == "failed"
    assert result["minted_lesson_ids"] == []
    assert result["evidence_id"] == "evidence-id"
    assert "checkpoint failed" in str(result["detail"])
