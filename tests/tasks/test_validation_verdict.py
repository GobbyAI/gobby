"""Structured task-validation verdict contracts."""

from __future__ import annotations

import pytest

from gobby.tasks.validation_verdict import (
    contradiction_rejection_message,
    demote_contradictory_valid,
    filter_failure_evidence,
    format_close_validation_message,
    is_contradictory_valid,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("pytest failed", []),
        ([" pytest failed ", "", "  ", None, 1, True], ["pytest failed"]),
        (["N/A", "none", "NULL", "real failure"], ["real failure"]),
    ],
)
def test_filter_failure_evidence_keeps_only_current_failure_strings(
    raw: object, expected: list[str]
) -> None:
    assert filter_failure_evidence(raw) == expected


def test_demote_contradictory_valid_records_structured_override() -> None:
    payload: dict[str, object] = {
        "status": "valid",
        "feedback": "All requested behavior is implemented.",
        "blocking_reasons": [],
        "current_failure_evidence": [" pytest: 1 failed ", "N/A"],
    }

    demoted = demote_contradictory_valid(payload)

    assert is_contradictory_valid("valid", ["pytest: 1 failed"])
    assert demoted == {
        **payload,
        "status": "invalid",
        "blocking_reasons": ["pytest: 1 failed"],
        "current_failure_evidence": ["pytest: 1 failed"],
        "verdict_override": {
            "from": "valid",
            "to": "invalid",
            "reason": "current_failure_evidence",
            "evidence": ["pytest: 1 failed"],
        },
    }


def test_filtered_empty_evidence_is_not_a_contradiction() -> None:
    payload: dict[str, object] = {
        "status": "valid",
        "current_failure_evidence": ["N/A", "  ", None],
    }

    assert demote_contradictory_valid(payload) == {
        **payload,
        "current_failure_evidence": [],
    }


def test_contradiction_rejection_message_is_actionable() -> None:
    assert contradiction_rejection_message({"current_failure_evidence": ["pytest failed"]}) == (
        "Contradictory validation verdict: current_failure_evidence attests that failures "
        "currently exist. Either return status='invalid' with blocking_reasons, or return "
        "an empty current_failure_evidence array if nothing is currently failing."
    )


def test_format_close_validation_message_is_mechanical_first_and_single_prefix() -> None:
    override: dict[str, object] = {
        "from": "valid",
        "to": "invalid",
        "reason": "current_failure_evidence",
        "evidence": ["pytest: 1 failed"],
    }

    message = format_close_validation_message(
        "invalid",
        "The implementation otherwise satisfies the requested behavior.",
        ["pytest: 1 failed"],
        override,
    )

    assert message == (
        "Close blocked: validation verdict 'invalid' — verdict overridden: validator attested "
        "current failures: pytest: 1 failed\n"
        "Blocking reasons: pytest: 1 failed\n\n"
        "Validator feedback:\n"
        "The implementation otherwise satisfies the requested behavior."
    )
    assert message.count("Close blocked:") == 1
