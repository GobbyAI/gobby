from __future__ import annotations

import pytest

from gobby.tasks.close_verdict import CloseVerdictParseError, parse_close_verdict

CRITERIA = (
    "Tests pass for supported providers.",
    "The close prompt stays bounded.",
)


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


def test_fuzzy_text_matches_when_index_is_missing() -> None:
    verdict = parse_close_verdict(
        {
            "status": "invalid",
            "criteria": [
                {
                    "criterion": "close prompt remains bounded",
                    "satisfied": False,
                    "gap": "Prompt exceeds the limit.",
                }
            ],
            "feedback": "One gap.",
        },
        CRITERIA,
    )

    assert verdict.criteria[0].satisfied is False
    assert verdict.criteria[0].gap == "One gap."
    assert verdict.criteria[1].gap == "Prompt exceeds the limit."


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("valid", [True, True]),
        ("invalid", [False, False]),
    ],
)
def test_missing_entries_inherit_overall_status(status: str, expected: list[bool]) -> None:
    verdict = parse_close_verdict(
        {"status": status, "criteria": [], "feedback": "overall"}, CRITERIA
    )

    assert [entry.satisfied for entry in verdict.criteria] == expected


def test_extra_entries_are_ignored() -> None:
    verdict = parse_close_verdict(
        {
            "status": "valid",
            "criteria": [
                {"index": 1, "satisfied": True},
                {"index": 2, "satisfied": True},
                {"index": 999, "satisfied": False, "gap": "invented"},
            ],
        },
        CRITERIA,
    )

    assert len(verdict.criteria) == 2
    assert verdict.valid is True


def test_contradictory_item_does_not_demote_overall_status() -> None:
    verdict = parse_close_verdict(
        {
            "status": "valid",
            "criteria": [{"index": 1, "satisfied": False, "gap": "model contradiction"}],
        },
        CRITERIA,
    )

    assert verdict.valid is True
    assert verdict.criteria[0].satisfied is False


def test_json_code_fence_and_surrounding_text_are_tolerated() -> None:
    verdict = parse_close_verdict(
        'Result:\n```json\n{"status":"VALID","criteria":[],"feedback":"ok"}\n```',
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
