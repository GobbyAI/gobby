"""Plan prose must not describe review tools as complete/fail shims."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_no_complete_stage_fail_stage_shim_prose_in_plan_files() -> None:
    plans = source_texts((".gobby/plans",))

    assert "mark_task_needs_review" not in plans or "complete_stage" not in plans
    assert "mark_task_review_approved" not in plans or "complete_stage" not in plans
    assert "mark_task_review_rejected" not in plans or "fail_stage" not in plans
