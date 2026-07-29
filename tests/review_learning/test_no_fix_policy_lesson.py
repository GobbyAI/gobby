from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.plans.review_ledger import merge_quality_ledger
from gobby.review_learning import recorders
from gobby.review_learning.recorders import mint_plan_review_lessons
from gobby.review_learning.round_diff import classify_plan_review_rounds
from gobby.storage.hub.protocol import HubDatabase
from tests.review_coverage_helpers import (
    StubReviewLearningService,
)
from tests.review_coverage_helpers import (
    ledger_finding as _ledger_finding,
)
from tests.review_coverage_helpers import (
    ledger_round_result as _round_result,
)
from tests.review_coverage_helpers import (
    review_evidence_row as _row,
)
from tests.review_coverage_helpers import (
    round_diff_finding as _round_finding,
)

TASK_ID = "task-lineage"
STAGE = "planning"


def _carried_ledger(rounds_carried: int = 3) -> list[dict[str, object]]:
    ledger: list[dict[str, object]] = []
    for round_number in range(1, rounds_carried + 1):
        ledger = merge_quality_ledger(
            prior_ledger=ledger,
            round_number=round_number,
            current_section_hashes={"A": "a" * 64},
            round_result=_round_result(
                findings=[
                    _ledger_finding(
                        f"major-{round_number}",
                        check_key="explicit-no-fix-policy",
                        section_ids=("A",),
                    )
                ]
            ),
        )
    return ledger


@pytest.mark.asyncio
async def test_carry_three_rounds_mints_lesson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def checkpoint(
        _service: object,
        _approval: object,
        *,
        status: str,
        lesson_ids: list[str],
        detail: str | None,
    ) -> dict[str, object]:
        return {
            "lesson_mint_status": status,
            "minted_lesson_ids": lesson_ids,
            "detail": detail,
        }

    approval = replace(
        _row(3, {"A": "a" * 64}, [], verdict="approved"),
        quality_ledger=_carried_ledger(),
    )
    rows = [
        _row(1, {"A": "a" * 64}, []),
        _row(2, {"A": "a" * 64}, []),
        approval,
    ]
    monkeypatch.setattr(
        PlanReviewEvidenceStore,
        "list_for_task_stage",
        lambda _self, **_kwargs: rows,
    )
    monkeypatch.setattr(
        recorders,
        "get_task",
        lambda _db, _task_id: SimpleNamespace(is_escalated=False),
    )
    monkeypatch.setattr(
        recorders,
        "_checkpoint",
        checkpoint,
    )
    recorder = StubReviewLearningService()

    result = await mint_plan_review_lessons(
        TASK_ID,
        STAGE,
        db=cast(HubDatabase, object()),
        review_learning_service=recorder,
    )

    assert result["lesson_mint_status"] == "minted"
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["decision"] == "no-fix-policy"
    assert recorder.calls[0]["finding"]["guardrail_target"] == "checklist"


def test_minting_eligibility_paths() -> None:
    blocking = classify_plan_review_rounds(
        [
            _row(1, {"A": "a" * 64}, []),
            _row(
                2,
                {"A": "a" * 64},
                [_round_finding("blocking", participating=["A"])],
            ),
        ],
        task_id=TASK_ID,
        stage=STAGE,
    )
    ordinary_major = classify_plan_review_rounds(
        [_row(1, {"A": "a" * 64}, [_round_finding("major", severity="major")])],
        task_id=TASK_ID,
        stage=STAGE,
    )
    carried_twice = classify_plan_review_rounds(
        [
            replace(
                _row(2, {"A": "a" * 64}, [], verdict="approved"),
                quality_ledger=_carried_ledger(2),
            )
        ],
        task_id=TASK_ID,
        stage=STAGE,
    )
    stale_ledger = [dict(entry) for entry in _carried_ledger()]
    stale_ledger[0]["stale"] = True
    stale_three = classify_plan_review_rounds(
        [
            replace(
                _row(3, {"A": "a" * 64}, [], verdict="approved"),
                quality_ledger=stale_ledger,
            )
        ],
        task_id=TASK_ID,
        stage=STAGE,
    )
    carried_three = classify_plan_review_rounds(
        [
            replace(
                _row(3, {"A": "a" * 64}, [], verdict="approved"),
                quality_ledger=_carried_ledger(),
            )
        ],
        task_id=TASK_ID,
        stage=STAGE,
    )

    assert blocking and all(candidate.decision == "confirmed" for candidate in blocking)
    assert ordinary_major == []
    assert carried_twice == []
    assert stale_three == []
    assert len(carried_three) == 1
    assert carried_three[0].decision == "no-fix-policy"
    assert carried_three[0].source == "quality_ledger"
    assert cast(dict[str, Any], carried_three[0].proof)["rounds_carried"] == 3
