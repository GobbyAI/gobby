"""Small builders and doubles for plan-review coverage tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from gobby.plans.review_coverage import REVIEW_LANES


class StubReviewLearningService:
    """Record `record()` calls, optionally failing, for review-lesson tests."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("recorder unavailable")
        return {"lesson_id": "lesson-1"}


def manifest_digest(entries: Sequence[Mapping[str, object]]) -> str:
    """Return the canonical digest used by the review manifest service."""
    return hashlib.sha256(
        json.dumps(list(entries), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def coverage_attestation(
    *,
    evidence_id: str = "test-evidence",
    manifest_entries: Sequence[Mapping[str, object]] | None = None,
    shadow_valid: bool = True,
) -> dict[str, object]:
    """Build a canonical signed attestation for tests outside coverage validation."""
    shadow: dict[str, object]
    if shadow_valid:
        entries = list(manifest_entries or [])
        shadow = {
            "status": "valid",
            "manifest_digest": manifest_digest(entries),
            "entry_count": len(entries),
        }
    else:
        shadow = {
            "status": "invalid",
            "diagnostics": [{"code": "test_shadow_failure", "message": "invalid"}],
        }
    payload: dict[str, object] = {
        "version": 1,
        "evidence_id": evidence_id,
        "lanes": [
            {
                "lane_id": lane_id,
                "status": (
                    "delegated-verified" if lane_id == "repository_blast_radius" else "completed"
                ),
                "candidate_count": 0,
            }
            for lane_id in REVIEW_LANES
        ],
        "source_digest": "0" * 64,
        "disposition_counts": {
            "total": 0,
            "emitted_findings": 0,
            "dismissed": 0,
        },
        "cross_lane_interaction_complete": True,
        "adjacent_variant_complete": True,
        "shadow_manifest_status": shadow,
    }
    payload["attestation_digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload
