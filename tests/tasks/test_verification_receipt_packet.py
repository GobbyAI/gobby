from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from gobby.storage.verification_receipts import VerificationOutcome, VerificationReceipt
from gobby.tasks.verification_receipt_packet import build_verification_receipt_packet
from gobby.utils.datetime import utc_now


def _receipt(
    index: int,
    *,
    outcome: VerificationOutcome = "success",
    task_id: str | None = "task-1",
    command_chars: int = 32,
) -> VerificationReceipt:
    timestamp = utc_now() + timedelta(seconds=index)
    output = f"output-{index}"
    return VerificationReceipt(
        id=f"receipt-{index:04d}",
        project_id="project-1",
        session_id="session-1",
        task_id=task_id,
        provider="codex",
        execution_id=f"execution-{index}",
        source_event_id=f"event-{index}",
        evidence_type="validation_command",
        command=f"uv run pytest {'x' * command_chars} {index}",
        cwd="/repo",
        normalized_outcome=outcome,
        outcome_provenance="structured_exit_code",
        exit_code=0 if outcome == "success" else 1 if outcome == "failure" else None,
        started_at=timestamp,
        completed_at=timestamp,
        output_first_4k=output,
        output_last_4k=output,
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        output_bytes=len(output),
        details={"index": index},
        attribution_source="sole_claim" if task_id else "unassigned",
        attribution_actor="session-1" if task_id else None,
        attributed_at=timestamp if task_id else None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _payload(text: str) -> dict[str, object]:
    return json.loads(text.removeprefix("Verification receipt packet:\n"))


def test_packet_is_bounded_complete_and_keeps_high_risk_receipts_visible() -> None:
    receipts = [_receipt(index) for index in range(300)]
    receipts.extend(
        [
            _receipt(300, outcome="failure", task_id=None),
            _receipt(301, outcome="unknown"),
            _receipt(302, outcome="conflicting"),
        ]
    )

    packet = build_verification_receipt_packet(receipts, unassigned_count=7)

    assert packet.error is None
    assert packet.text is not None
    assert len(packet.text) <= 32_000
    assert packet.disclosure.total == 303
    assert packet.disclosure.unassigned == 7
    assert packet.disclosure.aggregated > 0
    assert packet.disclosure.per_outcome == {
        "conflicting": 1,
        "failure": 1,
        "success": 300,
        "unknown": 1,
    }
    payload = _payload(packet.text)
    catalog_ids = {row["receipt_id"] for row in payload["receipt_catalog"]}
    assert {"receipt-0300", "receipt-0301", "receipt-0302"} <= catalog_ids
    assert payload["canonical_outcome_projection"] == packet.projection.to_dict()
    assert payload["evidence_completeness"] == packet.disclosure.to_dict()
    assert payload["aggregated_tail"][0]["count"] == packet.disclosure.aggregated


def test_explicit_receipt_is_first_detailed_before_high_risk_receipts() -> None:
    receipts = [
        _receipt(1, outcome="failure"),
        _receipt(2),
        _receipt(3, outcome="unknown"),
    ]

    packet = build_verification_receipt_packet(
        receipts,
        explicit_receipt_ids=["receipt-0002"],
    )

    assert packet.text is not None
    details = _payload(packet.text)["detailed_receipts"]
    assert details[0]["receipt_id"] == "receipt-0002"


def test_detailed_receipt_preserves_full_command_for_semantic_verification() -> None:
    receipt = _receipt(1, command_chars=700)

    packet = build_verification_receipt_packet([receipt], explicit_receipt_ids=[receipt.id])

    assert packet.text is not None
    payload = _payload(packet.text)
    assert payload["detailed_receipts"][0]["command"] == receipt.command


@pytest.mark.parametrize("outcome", ["failure", "conflicting", "unknown"])
def test_large_high_risk_history_is_aggregated_without_hiding_outcomes(
    outcome: VerificationOutcome,
) -> None:
    receipts = [_receipt(index, outcome=outcome, command_chars=4_000) for index in range(400)]

    packet = build_verification_receipt_packet(receipts)

    assert packet.error is None
    assert packet.text is not None
    assert len(packet.text) <= 32_000
    assert packet.disclosure.total == 400
    assert packet.disclosure.catalogued == 1
    assert packet.disclosure.aggregated == 399
    assert packet.disclosure.per_outcome == {outcome: 400}
    payload = _payload(packet.text)
    catalog = payload["receipt_catalog"]
    assert len(catalog) == 1
    assert catalog[0]["receipt_id"] == "receipt-0399"
    assert catalog[0]["outcome"] == outcome
    assert catalog[0]["exit_code"] == (1 if outcome == "failure" else None)
    assert len(catalog[0]["command"]) == 48
    assert catalog[0]["command"].endswith(" 399")
    assert catalog[0]["completed_at"] == receipts[-1].completed_at.isoformat()
    assert payload["aggregated_tail"][0]["outcomes"] == {outcome: 399}


def test_latest_success_is_detailed_before_historical_failures() -> None:
    receipts = [_receipt(index, outcome="failure") for index in range(30)]
    receipts.append(_receipt(30))

    packet = build_verification_receipt_packet(receipts)

    assert packet.text is not None
    details = _payload(packet.text)["detailed_receipts"]
    assert details[0]["receipt_id"] == "receipt-0030"
    assert details[0]["outcome"] == "success"


def test_packet_refuses_when_minimal_disclosure_exceeds_budget() -> None:
    packet = build_verification_receipt_packet([_receipt(1)], budget_chars=1)

    assert packet.text is None
    assert packet.error == "evidence_budget_exceeded"


def test_empty_packet_reports_zero_completeness() -> None:
    packet = build_verification_receipt_packet([], unassigned_count=4)

    assert packet.error is None
    assert packet.text is not None
    assert packet.disclosure.to_dict() == {
        "total": 0,
        "detailed": 0,
        "catalogued": 0,
        "aggregated": 0,
        "unassigned": 4,
        "per_outcome": {},
    }
