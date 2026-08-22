"""Error precedence for ``append_plan_changelog_round``."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from tests.plans.review_evidence_helpers import (
    ROUND_PROSE,
    bind_interactive_review,
    repair_reviewed_section,
)


def test_append_without_round_result_reports_missing_payload_before_byte_check(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    evidence_id = bind_interactive_review(service, project_id, session_id, plan_path)
    repair_reviewed_section(plan_path)
    before = plan_path.read_bytes()

    with pytest.raises(ReviewEvidenceError) as missing:
        service.append_plan_changelog_round(evidence_id, ROUND_PROSE)

    assert missing.value.code == "missing_round_result"
    assert plan_path.read_bytes() == before
