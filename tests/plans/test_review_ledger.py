from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_findings import CHECK_KEY_RE, FINDING_CATEGORIES
from gobby.plans.review_ledger import (
    inject_dismissed_ledger_context,
    merge_quality_ledger,
    validate_quality_ledger,
)
from tests.review_coverage_helpers import (
    ledger_finding as _finding,
)
from tests.review_coverage_helpers import (
    ledger_round_result as _round_result,
)


def _disposition(
    candidate_id: str,
    *,
    disposition: str = "dismissed",
    finding_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "candidate_id": candidate_id,
        "check_key": "candidate-parity",
        "source_section_ids": ["1.1"],
        "source_hash": "c" * 64,
        "disposition": disposition,
        "rationale": "The candidate duplicates an existing invariant.",
    }
    if finding_id is not None:
        record["finding_id"] = finding_id
    return record


def _carry(*finding_ids: str) -> dict[str, object]:
    return {
        "prior_finding_resolutions": [
            {"prior_finding_id": finding_id, "decision": "carry"} for finding_id in finding_ids
        ]
    }


def _active(ledger: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [entry for entry in ledger if entry["stale"] is False]


def test_merge_and_staleness_across_rounds() -> None:
    hashes = {"1.1": "a" * 64}
    round_one = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes=hashes,
        round_result=_round_result(findings=[_finding("finding-r1")]),
    )
    first_id = round_one[0]["ledger_entry_id"]
    assert round_one[0]["first_seen_round"] == 1
    assert round_one[0]["rounds_carried"] == 1

    round_two = merge_quality_ledger(
        prior_ledger=round_one,
        round_number=2,
        current_section_hashes=hashes,
        round_result=_round_result(),
        prior_round_context=_carry("finding-r1"),
    )
    assert len(round_two) == 1
    assert round_two[0]["ledger_entry_id"] == first_id
    assert round_two[0]["rounds_carried"] == 2

    changed_hashes = {"1.1": "b" * 64}
    round_three = merge_quality_ledger(
        prior_ledger=round_two,
        round_number=3,
        current_section_hashes=changed_hashes,
        round_result=_round_result(
            findings=[
                _finding(
                    "finding-r3",
                    description="The reworded issue remains on the changed surface.",
                )
            ]
        ),
    )
    assert len(round_three) == 2
    assert round_three[0]["ledger_entry_id"] == first_id
    assert round_three[0]["stale"] is True
    active = _active(round_three)
    assert len(active) == 1
    assert active[0]["ledger_entry_id"] != first_id
    assert active[0]["first_seen_round"] == 3
    assert active[0]["rounds_carried"] == 1


def test_canonical_coalescing_order_and_hash_split() -> None:
    hashes = {"1.1": "a" * 64, "2.1": "b" * 64}
    round_one = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes=hashes,
        round_result=_round_result(findings=[_finding("finding-one", section_ids=("2.1", "1.1"))]),
    )
    first_id = round_one[0]["ledger_entry_id"]

    round_two = merge_quality_ledger(
        prior_ledger=round_one,
        round_number=2,
        current_section_hashes=hashes,
        round_result=_round_result(
            findings=[
                _finding(
                    "finding-two",
                    section_ids=("1.1", "2.1"),
                    description="Same invariant, reworded with a new local ID.",
                )
            ]
        ),
    )
    assert len(round_two) == 1
    assert round_two[0]["ledger_entry_id"] == first_id
    assert round_two[0]["aliases"] == ["finding-one", "finding-two"]
    assert round_two[0]["source_section_ids"] == ["1.1", "2.1"]
    assert round_two[0]["rounds_carried"] == 2

    changed_hashes = {"1.1": "a" * 64, "2.1": "d" * 64}
    round_three = merge_quality_ledger(
        prior_ledger=round_two,
        round_number=3,
        current_section_hashes=changed_hashes,
        round_result=_round_result(
            findings=[_finding("finding-three", section_ids=("2.1", "1.1"))]
        ),
    )
    active = _active(round_three)
    assert len(active) == 1
    assert active[0]["ledger_entry_id"] != first_id
    assert active[0]["section_hashes_at_entry"] == {
        "1.1": "a" * 64,
        "2.1": "d" * 64,
    }
    assert next(entry for entry in round_three if entry["ledger_entry_id"] == first_id)["stale"]


def test_dismissed_entries_from_canonical_result() -> None:
    result = _round_result(dispositions=[_disposition("candidate-one")])
    ledger = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes={"1.1": "a" * 64},
        round_result=result,
    )

    assert ledger == validate_quality_ledger(ledger)
    assert ledger[0]["kind"] == "dismissed"
    assert ledger[0]["aliases"] == ["candidate-one"]
    assert ledger[0]["source_hash"] == "c" * 64
    assert ledger[0]["rationale"] == "The candidate duplicates an existing invariant."

    with pytest.raises(ReviewEvidenceError, match="disposition_counts"):
        _round_result(
            dispositions=[_disposition("candidate-one")],
            counts={"total": 2, "emitted_findings": 0, "dismissed": 2},
        )


def test_dismissal_injection_and_reopen_rule() -> None:
    hashes = {"1.1": "a" * 64}
    ledger = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes=hashes,
        round_result=_round_result(dispositions=[_disposition("candidate-one")]),
    )

    unchanged = inject_dismissed_ledger_context(
        prior_round_context={"prior_evidence_id": "evidence-1"},
        prior_ledger=ledger,
        current_section_hashes=hashes,
    )
    entries = unchanged["dismissed_ledger_entries"]
    assert isinstance(entries, list)
    assert entries[0]["aliases"] == ["candidate-one"]
    assert entries[0]["reopenable"] is False
    assert entries[0]["source_hash"] == "c" * 64
    assert entries[0]["section_hashes_at_entry"] == hashes

    changed = inject_dismissed_ledger_context(
        prior_round_context={"prior_evidence_id": "evidence-1"},
        prior_ledger=ledger,
        current_section_hashes={"1.1": "b" * 64},
    )
    changed_entries = changed["dismissed_ledger_entries"]
    assert isinstance(changed_entries, list)
    assert changed_entries[0]["reopenable"] is True


def test_ledger_validation_shares_finding_vocabularies() -> None:
    assert "unhandled-edge" in FINDING_CATEGORIES
    assert CHECK_KEY_RE.fullmatch("consumer-parity")

    with pytest.raises(ReviewEvidenceError, match="category"):
        merge_quality_ledger(
            prior_ledger=[],
            round_number=1,
            current_section_hashes={"1.1": "a" * 64},
            round_result=_round_result(
                findings=[_finding("bad-category", category="invented-category")]
            ),
        )
    with pytest.raises(ReviewEvidenceError, match="check_key"):
        merge_quality_ledger(
            prior_ledger=[],
            round_number=1,
            current_section_hashes={"1.1": "a" * 64},
            round_result=_round_result(
                findings=[_finding("bad-check", check_key="contains spaces")]
            ),
        )

    new_deliverable = _finding("new-runbook", repair_scope="new_deliverable")
    new_deliverable["new_deliverable_justification"] = (
        "No existing plan section owns operator documentation."
    )
    ledger = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes={"1.1": "a" * 64},
        round_result=_round_result(findings=[new_deliverable]),
    )
    assert ledger[0]["repair_scope"] == "new_deliverable"
    assert (
        ledger[0]["new_deliverable_justification"]
        == "No existing plan section owns operator documentation."
    )

    with pytest.raises(ReviewEvidenceError, match="repair_scope"):
        merge_quality_ledger(
            prior_ledger=[],
            round_number=1,
            current_section_hashes={"1.1": "a" * 64},
            round_result=_round_result(findings=[_finding("bad-scope", repair_scope="whole_plan")]),
        )


def test_carry_across_changed_hash_preserves_history() -> None:
    round_one = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes={"1.1": "a" * 64},
        round_result=_round_result(findings=[_finding("finding-r1")]),
    )

    round_two = merge_quality_ledger(
        prior_ledger=round_one,
        round_number=2,
        current_section_hashes={"1.1": "b" * 64},
        round_result=_round_result(),
        prior_round_context=_carry("finding-r1"),
    )

    active = _active(round_two)
    assert len(active) == 1
    assert active[0]["first_seen_round"] == 1
    assert active[0]["rounds_carried"] == 2


def test_missing_carry_section_stales_source_without_replacement() -> None:
    round_one = merge_quality_ledger(
        prior_ledger=[],
        round_number=1,
        current_section_hashes={"1.1": "a" * 64},
        round_result=_round_result(findings=[_finding("finding-r1")]),
    )

    round_two = merge_quality_ledger(
        prior_ledger=round_one,
        round_number=2,
        current_section_hashes={},
        round_result=_round_result(),
        prior_round_context=_carry("finding-r1"),
    )

    assert len(round_two) == 1
    assert round_two[0]["stale"] is True


def test_finding_for_removed_section_is_skipped() -> None:
    ledger = merge_quality_ledger(
        prior_ledger=[],
        round_number=2,
        current_section_hashes={"2.1": "b" * 64},
        round_result=_round_result(findings=[_finding("removed-section")]),
    )

    assert ledger == []
