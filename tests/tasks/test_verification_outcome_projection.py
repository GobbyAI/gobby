"""Tests for the provider-neutral durable receipt projection."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gobby.storage.verification_receipts import VerificationOutcome, VerificationReceipt
from gobby.tasks.verification_outcome_projection import project_verification_outcomes
from gobby.utils.datetime import utc_now

pytestmark = pytest.mark.unit


def _receipt(
    index: int,
    outcome: VerificationOutcome,
    *,
    provider: str = "codex",
    command: str = "custom verifier --selector value",
) -> VerificationReceipt:
    timestamp = utc_now() + timedelta(seconds=index)
    return VerificationReceipt(
        id=f"receipt-{index}",
        project_id="project-1",
        session_id="session-1",
        task_id="task-1",
        provider=provider,
        execution_id=f"execution-{index}",
        source_event_id=f"event-{index}",
        evidence_type="shell_command",
        command=command,
        cwd="/repo",
        normalized_outcome=outcome,
        outcome_provenance="provider-contract",
        exit_code=0 if outcome == "success" else 1 if outcome == "failure" else None,
        started_at=timestamp,
        completed_at=timestamp,
        output_first_4k="output",
        output_last_4k="output",
        output_sha256=None,
        output_bytes=6,
        details={},
        attribution_source="sole_claim",
        attribution_actor="session-1",
        attributed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_projection_is_deterministic_and_provider_neutral() -> None:
    receipts = [
        _receipt(2, "unknown", provider="agy", command="opaque command"),
        _receipt(0, "success", provider="claude", command="pytest -k selector"),
        _receipt(1, "failure", provider="grok", command="custom verifier"),
    ]

    projection = project_verification_outcomes(receipts)
    replay = project_verification_outcomes(list(reversed(receipts)))

    assert [receipt.id for receipt in projection.receipts] == [
        "receipt-0",
        "receipt-1",
        "receipt-2",
    ]
    assert projection.to_dict() == replay.to_dict()
    assert projection.per_outcome == {"failure": 1, "success": 1, "unknown": 1}
    assert projection.ready is True
    assert projection.latest_receipt_id == "receipt-2"


@pytest.mark.parametrize("outcome", ["failure", "unknown", "conflicting"])
def test_projection_requires_a_durable_success(outcome: VerificationOutcome) -> None:
    projection = project_verification_outcomes([_receipt(0, outcome)])

    assert projection.ready is False
    assert projection.per_outcome == {outcome: 1}


def test_empty_projection_is_not_ready() -> None:
    projection = project_verification_outcomes([])

    assert projection.to_dict() == {
        "total": 0,
        "per_outcome": {},
        "ready": False,
        "latest_receipt_id": None,
        "latest_timestamp": None,
    }
