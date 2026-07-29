from __future__ import annotations

import pytest

from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_sweeps import validate_candidate_dispositions

pytestmark = pytest.mark.unit


def _disposition(candidate_id: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "check_key": "check.one",
        "source_section_ids": ["1.1"],
        "source_hash": "a" * 64,
        "rationale": "Candidate was reviewed.",
        "disposition": "dismissed",
    }


def test_candidate_disposition_errors_distinguish_duplicate_from_unknown() -> None:
    record = _disposition("candidate-1")

    with pytest.raises(ReviewEvidenceError, match="duplicate candidate disposition"):
        validate_candidate_dispositions([record, dict(record)], candidates=None)

    with pytest.raises(ReviewEvidenceError, match="unknown candidate disposition"):
        validate_candidate_dispositions([record], candidates={})
