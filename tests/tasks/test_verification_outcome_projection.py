"""Tests for the provider-neutral durable receipt projection."""

from __future__ import annotations

import hashlib
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
    execution_id: str | None = None,
    outcome_provenance: str = "provider-contract",
    output: str = "output",
) -> VerificationReceipt:
    timestamp = utc_now() + timedelta(seconds=index)
    return VerificationReceipt(
        id=f"receipt-{index}",
        project_id="project-1",
        session_id="session-1",
        task_id="task-1",
        provider=provider,
        execution_id=execution_id or f"execution-{index}",
        source_event_id=f"event-{index}",
        evidence_type="shell_command",
        command=command,
        cwd="/repo",
        normalized_outcome=outcome,
        outcome_provenance=outcome_provenance,
        exit_code=0 if outcome == "success" else 1 if outcome == "failure" else None,
        started_at=timestamp,
        completed_at=timestamp,
        output_first_4k=output,
        output_last_4k=output,
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        output_bytes=len(output),
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
        "raw_total": 0,
        "raw_per_outcome": {},
        "superseded_total": 0,
        "ready": False,
        "latest_receipt_id": None,
        "latest_timestamp": None,
    }


def test_nested_execution_id_supersedes_before_tool_receipt() -> None:
    candidate = _receipt(
        0,
        "unknown",
        command="uv run pytest tests/focused.py",
        execution_id="call-paired",
        outcome_provenance="before_tool",
        output="wrapper output",
    )
    authority = _receipt(
        1,
        "success",
        command="uv run pytest tests/focused.py",
        execution_id="call-paired:0",
        output="authoritative output",
    )

    projection = project_verification_outcomes([authority, candidate])

    assert [receipt.id for receipt in projection.receipts] == [authority.id]
    assert projection.superseded_by == {candidate.id: authority.id}
    assert projection.resolve_receipt_id(candidate.id) == authority.id
    assert projection.per_outcome == {"success": 1}
    assert projection.raw_per_outcome == {"success": 1, "unknown": 1}


def test_command_and_output_digest_supersede_before_tool_receipt_deterministically() -> None:
    command = "uv run ruff check src/gobby/tasks"
    candidate = _receipt(
        0,
        "unknown",
        command=command,
        execution_id="exec-wrapper",
        outcome_provenance="before_tool",
        output="All checks passed!\n",
    )
    first_authority = _receipt(
        1,
        "success",
        command=command,
        execution_id="call-first:0",
        output="All checks passed!\n",
    )
    later_authority = _receipt(
        2,
        "success",
        command=command,
        execution_id="call-later:0",
        output="All checks passed!\n",
    )

    projection = project_verification_outcomes([later_authority, candidate, first_authority])
    replay = project_verification_outcomes([first_authority, candidate, later_authority])

    assert projection.superseded_by == {candidate.id: first_authority.id}
    assert projection.to_dict() == replay.to_dict()
    assert projection.total == 2
    assert projection.raw_total == 3
    assert projection.superseded_total == 1


def test_authoritative_failure_supersedes_matching_unknown() -> None:
    command = "uv run pytest tests/failing.py"
    candidate = _receipt(
        0,
        "unknown",
        command=command,
        execution_id="exec-failure",
        outcome_provenance="before_tool",
        output="1 failed\n",
    )
    authority = _receipt(
        1,
        "failure",
        command=command,
        execution_id="call-failure:0",
        output="1 failed\n",
    )

    projection = project_verification_outcomes([candidate, authority])

    assert [receipt.id for receipt in projection.receipts] == [authority.id]
    assert projection.per_outcome == {"failure": 1}
    assert projection.ready is False


def test_conflicting_matching_authorities_preserve_unknown_candidate() -> None:
    command = "custom verifier"
    candidate = _receipt(
        0,
        "unknown",
        command=command,
        execution_id="exec-conflict",
        outcome_provenance="before_tool",
        output="same output",
    )
    success = _receipt(
        1,
        "success",
        command=command,
        execution_id="call-success:0",
        output="same output",
    )
    failure = _receipt(
        2,
        "failure",
        command=command,
        execution_id="call-failure:0",
        output="same output",
    )

    projection = project_verification_outcomes([candidate, success, failure])

    assert [receipt.id for receipt in projection.receipts] == [
        candidate.id,
        success.id,
        failure.id,
    ]
    assert projection.superseded_total == 0
    assert projection.per_outcome == {"failure": 1, "success": 1, "unknown": 1}


def test_different_output_and_unknown_only_receipts_remain_effective() -> None:
    command = "custom verifier"
    candidate = _receipt(
        0,
        "unknown",
        command=command,
        execution_id="exec-unmatched",
        outcome_provenance="before_tool",
        output="candidate output",
    )
    authority = _receipt(
        1,
        "success",
        command=command,
        execution_id="call-unrelated:0",
        output="different output",
    )
    unknown_only = _receipt(
        2,
        "provisional",
        command="another verifier",
        execution_id="exec-unknown-only",
        outcome_provenance="before_tool",
        output="",
    )

    projection = project_verification_outcomes([candidate, authority, unknown_only])

    assert [receipt.id for receipt in projection.receipts] == [
        candidate.id,
        authority.id,
        unknown_only.id,
    ]
    assert projection.superseded_total == 0
    assert projection.per_outcome == {"provisional": 1, "success": 1, "unknown": 1}
