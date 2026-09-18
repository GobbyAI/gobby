from __future__ import annotations

import pytest

from gobby.tasks.close_verdict import CloseVerdictParseError, parse_close_verdict
from gobby.tasks.criteria_contract import split_validation_criteria

CRITERIA = (
    "Tests pass for supported providers.",
    "The close prompt stays bounded.",
)


def test_inline_criteria_preserve_third_gap_as_distinct_verdict() -> None:
    criteria = split_validation_criteria(
        "1. Focused tests pass. 2. Static checks pass. 3. The third gap is fixed."
    )
    verdict = parse_close_verdict(
        {
            "status": "invalid",
            "criteria": [
                {"index": 1, "satisfied": True},
                {"index": 2, "satisfied": True},
                {
                    "index": 3,
                    "satisfied": False,
                    "gap": "The third gap remains.",
                },
            ],
            "feedback": "One criterion remains unsatisfied.",
        },
        criteria,
    )

    assert len(verdict.criteria) == 3
    assert [entry.satisfied for entry in verdict.criteria] == [True, True, False]
    assert verdict.criteria[2].criterion == "The third gap is fixed."
    assert verdict.criteria[2].gap == "The third gap remains."


def test_status_is_case_insensitive_and_entries_match_by_index() -> None:
    verdict = parse_close_verdict(
        {
            "status": " VALID ",
            "criteria": [
                {"index": "2", "satisfied": "yes", "gap": None},
                {"index": 1, "satisfied": True},
            ],
            "feedback": "Looks coherent.",
        },
        CRITERIA,
    )

    assert verdict.valid is True
    assert [entry.index for entry in verdict.criteria] == [1, 2]
    assert all(entry.satisfied for entry in verdict.criteria)
    assert all(entry.required_evidence is None for entry in verdict.criteria)


def test_required_evidence_accepts_string_and_null() -> None:
    verdict = parse_close_verdict(
        {
            "status": "invalid",
            "criteria": [
                {
                    "index": 1,
                    "satisfied": False,
                    "gap": "Exercise the close adapter.",
                    "required_evidence": "Invoke close_task with the real rule and capture its receipt.",
                },
                {
                    "index": 2,
                    "satisfied": False,
                    "gap": "Document the prompt contract.",
                    "required_evidence": None,
                },
            ],
            "feedback": "Two criteria remain incomplete.",
        },
        CRITERIA,
    )

    assert verdict.criteria[0].required_evidence == (
        "Invoke close_task with the real rule and capture its receipt."
    )
    assert verdict.criteria[1].required_evidence is None


def test_pending_external_state_is_neither_satisfied_nor_a_gap() -> None:
    verdict = parse_close_verdict(
        {
            "status": "valid",
            "criteria": [
                {
                    "index": 1,
                    "state": "pending_external",
                    "satisfied": False,
                    "gap": "must be ignored",
                    "required_evidence": "must also be ignored",
                }
            ],
            "feedback": "Implementation criteria passed.",
        },
        ("Live: restart the daemon.",),
        defer_external_criteria=True,
    )

    criterion = verdict.criteria[0]
    assert criterion.verdict_state == "pending_external"
    assert criterion.satisfied is False
    assert criterion.gap is None
    assert criterion.required_evidence is None
    assert criterion.to_dict()["state"] == "pending_external"


def test_spawned_agent_forces_live_criterion_to_pending_external() -> None:
    verdict = parse_close_verdict(
        {
            "status": "invalid",
            "criteria": [
                {
                    "index": 1,
                    "state": "gap",
                    "satisfied": False,
                    "gap": "The agent cannot restart the daemon.",
                },
                {"index": 2, "state": "satisfied", "satisfied": True, "gap": None},
            ],
            "feedback": "Live verification unavailable.",
        },
        ("Live: restart the daemon.", "Focused tests pass."),
        defer_external_criteria=True,
    )

    assert [criterion.verdict_state for criterion in verdict.criteria] == [
        "pending_external",
        "satisfied",
    ]
    assert verdict.criteria[0].gap is None


def test_pending_external_rejected_for_non_agent_caller() -> None:
    with pytest.raises(CloseVerdictParseError, match="criterion 1: pending_external"):
        parse_close_verdict(
            {
                "status": "valid",
                "criteria": [
                    {"index": 1, "state": "pending_external", "satisfied": False, "gap": None},
                    {"index": 2, "state": "satisfied", "satisfied": True, "gap": None},
                ],
                "feedback": "Live criterion is coordinator-owned.",
            },
            ("Live: restart the daemon.", "Focused tests pass."),
        )


def test_pending_external_rejected_for_implementer_owned_criterion() -> None:
    with pytest.raises(CloseVerdictParseError, match="criterion 2: pending_external"):
        parse_close_verdict(
            {
                "status": "valid",
                "criteria": [
                    {"index": 1, "state": "pending_external", "satisfied": False, "gap": None},
                    {"index": 2, "state": "pending_external", "satisfied": False, "gap": None},
                ],
                "feedback": "Deferred everything.",
            },
            ("Live: restart the daemon.", "Focused tests pass."),
            defer_external_criteria=True,
        )


def test_fuzzy_text_matches_when_index_is_missing() -> None:
    verdict = parse_close_verdict(
        {
            "status": "invalid",
            "criteria": [
                {"index": 1, "satisfied": True},
                {
                    "criterion": "close prompt remains bounded",
                    "satisfied": False,
                    "gap": "Prompt exceeds the limit.",
                },
            ],
            "feedback": "One gap.",
        },
        CRITERIA,
    )

    assert verdict.criteria[0].satisfied is True
    assert verdict.criteria[1].satisfied is False
    assert verdict.criteria[1].gap == "Prompt exceeds the limit."


def test_text_only_entry_matching_no_criterion_is_rejected() -> None:
    with pytest.raises(CloseVerdictParseError, match="matching no criterion"):
        parse_close_verdict(
            {
                "status": "valid",
                "criteria": [
                    {"index": 1, "satisfied": True},
                    {"index": 2, "satisfied": True},
                    {"criterion": "zzzz qqqq vvvv", "satisfied": True},
                ],
                "feedback": "ok",
            },
            CRITERIA,
        )


@pytest.mark.parametrize("status", ["valid", "invalid"])
def test_missing_index_is_rejected(status: str) -> None:
    with pytest.raises(CloseVerdictParseError, match="every criterion index exactly once"):
        parse_close_verdict(
            {
                "status": status,
                "criteria": [{"index": 1, "satisfied": True, "gap": None}],
                "feedback": "overall",
            },
            CRITERIA,
        )


@pytest.mark.parametrize("status", ["valid", "invalid"])
def test_duplicate_index_is_rejected(status: str) -> None:
    with pytest.raises(CloseVerdictParseError, match=r"received \[1, 1\]"):
        parse_close_verdict(
            {
                "status": status,
                "criteria": [
                    {"index": 1, "satisfied": True, "gap": None},
                    {"index": 1, "satisfied": True, "gap": None},
                ],
                "feedback": "overall",
            },
            CRITERIA,
        )


@pytest.mark.parametrize("status", ["valid", "invalid"])
def test_extra_index_is_rejected(status: str) -> None:
    with pytest.raises(CloseVerdictParseError, match=r"received \[1, 2, 999\]"):
        parse_close_verdict(
            {
                "status": status,
                "criteria": [
                    {"index": 1, "satisfied": True, "gap": None},
                    {"index": 2, "satisfied": True, "gap": None},
                    {"index": 999, "satisfied": False, "gap": "invented"},
                ],
                "feedback": "overall",
            },
            CRITERIA,
        )


def test_rejection_names_expected_and_received_index_sets() -> None:
    with pytest.raises(CloseVerdictParseError) as excinfo:
        parse_close_verdict(
            {
                "status": "valid",
                "criteria": [{"index": 2, "satisfied": True, "gap": None}],
                "feedback": "overall",
            },
            CRITERIA,
        )

    assert "expected [1, 2]" in str(excinfo.value)
    assert "received [2]" in str(excinfo.value)


def test_nested_bullet_criteria_require_every_normalized_index() -> None:
    criteria = split_validation_criteria("- A top\n  - A nested one\n  - A nested two\n- B top")
    assert len(criteria) == 4

    with pytest.raises(CloseVerdictParseError, match=r"expected \[1, 2, 3, 4\]"):
        parse_close_verdict(
            {
                "status": "valid",
                "criteria": [{"index": 1, "satisfied": True, "gap": None}],
                "feedback": "Everything looks done.",
            },
            criteria,
        )


def test_contradictory_item_does_not_demote_overall_status() -> None:
    verdict = parse_close_verdict(
        {
            "status": "valid",
            "criteria": [
                {"index": 1, "satisfied": False, "gap": "model contradiction"},
                {"index": 2, "satisfied": True, "gap": None},
            ],
        },
        CRITERIA,
    )

    assert verdict.valid is True
    assert verdict.criteria[0].satisfied is False


def test_json_code_fence_and_surrounding_text_are_tolerated() -> None:
    verdict = parse_close_verdict(
        'Result:\n```json\n{"status":"VALID","criteria":'
        '[{"index":1,"satisfied":true,"gap":null},'
        '{"index":2,"satisfied":true,"gap":null}],"feedback":"ok"}\n```',
        CRITERIA,
    )

    assert verdict.valid is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "",
        "not json",
        '{"feedback": "missing status"}',
        '{"status": "pending"}',
    ],
)
def test_unparseable_response_is_infrastructure_failure(payload: object) -> None:
    with pytest.raises(CloseVerdictParseError):
        parse_close_verdict(payload, CRITERIA)
