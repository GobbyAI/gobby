"""Pure validation tests for typed repairs on plan-review findings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jsonschema.validators import validator_for

from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    SectionHash,
)
from gobby.plans.review_findings import (
    FINDING_CATEGORIES,
    FINDING_ITEM_SCHEMA,
    render_rejection_section,
    validate_plan_review_findings,
)
from gobby.plans.review_repairs import (
    REPAIR_KINDS,
    REPAIR_KINDS_BY_CATEGORY,
    validate_finding_repairs,
)

SECTION_IDS = {"P1", "1.1", "1.2", "2.1"}

_REPAIR_BY_KIND: dict[str, dict[str, object]] = {
    "add_targets": {
        "kind": "add_targets",
        "section_id": "1.2",
        "entries": ["`src/example.py::build`", "src/other.py::* — scope-reason: rewrite"],
    },
    "add_dependency": {"kind": "add_dependency", "section_id": "1.2", "on": ["1.1"]},
    "add_acceptance": {
        "kind": "add_acceptance",
        "section_id": "1.2",
        "items": [{"prose": "Rollback covered", "artifact": "test: `tests/test_x.py::test_r`"}],
    },
}


def _evidence() -> PlanReviewEvidence:
    created_at = datetime(2026, 8, 22, tzinfo=UTC)
    return PlanReviewEvidence(
        evidence_id="evidence-1",
        project_id="project-1",
        plan_path=".gobby/plans/review.md",
        plan_hash="plan-hash",
        section_manifest=tuple(
            SectionHash(section_id=section_id, section_hash=f"hash-{section_id}")
            for section_id in sorted(SECTION_IDS)
        ),
        snapshot=b"snapshot",
        round_number=1,
        session_id="session-1",
        task_id=None,
        stage=None,
        dispatch_run_id="run-1",
        lease_expires_at=None,
        finalized_at=None,
        expired_at=None,
        round_result=None,
        approval_result=None,
        approved_at=None,
        lesson_mint_status=None,
        lesson_mint_detail=None,
        manifest_digest=None,
        manifest_payload=None,
        manifest_state=None,
        manifest_result=None,
        manifest_applied_at=None,
        created_at=created_at,
    )


def _finding(**overrides: object) -> dict[str, object]:
    finding: dict[str, object] = {
        "finding_id": "F1",
        "section_id": "1.1",
        "check_key": "targets-complete",
        "severity": "blocking",
        "category": "traceability",
        "location": "§ 1.1 Targets",
        "description": "The consumer file is missing from Targets.",
        "fix": "Add the consumer to Targets.",
        "prevention": "Run the consumer sweep.",
        "root_cause": "Only direct edits were inventoried.",
    }
    finding.update(overrides)
    return finding


def test_category_matrix() -> None:
    assert REPAIR_KINDS == ("add_targets", "add_dependency", "add_acceptance")
    assert REPAIR_KINDS_BY_CATEGORY == {
        "traceability": frozenset({"add_targets", "add_acceptance"}),
        "bad-sequencing": frozenset({"add_dependency"}),
        "gobby-format": frozenset(REPAIR_KINDS),
        "weak-testability": frozenset({"add_acceptance"}),
    }
    for category in sorted(FINDING_CATEGORIES):
        allowed = REPAIR_KINDS_BY_CATEGORY.get(category, frozenset())
        for kind in REPAIR_KINDS:
            repairs = [dict(_REPAIR_BY_KIND[kind])]
            if kind in allowed:
                canonical = validate_finding_repairs(
                    repairs,
                    prefix="findings[0]",
                    category=category,
                    section_ids=SECTION_IDS,
                )
                assert canonical == repairs
                continue
            with pytest.raises(ReviewEvidenceError) as excinfo:
                validate_finding_repairs(
                    repairs,
                    prefix="findings[0]",
                    category=category,
                    section_ids=SECTION_IDS,
                )
            assert excinfo.value.code == "invalid_round_result"
            expected = "findings[0].repairs" if not allowed else "findings[0].repairs[0].kind"
            assert expected in str(excinfo.value)


@pytest.mark.parametrize(
    ("repairs", "prefix_fragment"),
    [
        pytest.param([], "findings[3].repairs", id="empty-list"),
        pytest.param("add_targets", "findings[3].repairs", id="not-a-list"),
        pytest.param(["add_targets"], "findings[3].repairs[0]", id="entry-not-object"),
        pytest.param(
            [{**_REPAIR_BY_KIND["add_targets"], "extra": 1}],
            "findings[3].repairs[0]",
            id="unknown-key",
        ),
        pytest.param(
            [{"kind": "rename", "section_id": "1.2", "entries": ["a.py"]}],
            "findings[3].repairs[0].kind",
            id="unknown-kind",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "9.9", "entries": ["`a.py`"]}],
            "findings[3].repairs[0].section_id",
            id="section-not-in-manifest",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "1.2", "entries": ["`a.py`"], "on": ["1.1"]}],
            "findings[3].repairs[0]",
            id="two-payload-keys",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "1.2", "entries": []}],
            "findings[3].repairs[0].entries",
            id="empty-entries",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "1.2", "entries": ["`a.py::`"]}],
            "findings[3].repairs[0].entries[0]",
            id="malformed-entry",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "1.2", "entries": ["`a.py::*`"]}],
            "findings[3].repairs[0].entries[0]",
            id="wildcard-without-reason",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "1.2", "entries": ["`a.py`, `b.py`"]}],
            "findings[3].repairs[0].entries[0]",
            id="entry-with-two-targets",
        ),
        pytest.param(
            [{"kind": "add_targets", "section_id": "1.2", "entries": ["`a.py::x`", "a.py::x"]}],
            "findings[3].repairs[0].entries[1]",
            id="duplicate-reference",
        ),
        pytest.param(
            [{"kind": "add_dependency", "section_id": "1.2", "on": ["1.1", "1.1"]}],
            "findings[3].repairs[0].on[1]",
            id="duplicate-dependency",
        ),
        pytest.param(
            [{"kind": "add_dependency", "section_id": "1.2", "on": ["9.9"]}],
            "findings[3].repairs[0].on[0]",
            id="dependency-not-in-manifest",
        ),
        pytest.param(
            [{"kind": "add_dependency", "section_id": "1.2", "on": ["1.2"]}],
            "findings[3].repairs[0].on[0]",
            id="self-dependency",
        ),
        pytest.param(
            [{"kind": "add_acceptance", "section_id": "1.2", "items": [{"prose": "x"}]}],
            "findings[3].repairs[0].items[0]",
            id="item-missing-artifact",
        ),
        pytest.param(
            [
                {
                    "kind": "add_acceptance",
                    "section_id": "1.2",
                    "items": [{"prose": "a\nb", "artifact": "file: `a.py`"}],
                }
            ],
            "findings[3].repairs[0].items[0].prose",
            id="multiline-prose",
        ),
        pytest.param(
            [
                {
                    "kind": "add_acceptance",
                    "section_id": "1.2",
                    "items": [{"prose": "a", "artifact": "doc: `a.md`"}],
                }
            ],
            "findings[3].repairs[0].items[0].artifact",
            id="artifact-kind-unknown",
        ),
    ],
)
def test_shape_errors_name_their_prefix(repairs: object, prefix_fragment: str) -> None:
    with pytest.raises(ReviewEvidenceError) as excinfo:
        validate_finding_repairs(
            repairs,
            prefix="findings[3]",
            category="gobby-format",
            section_ids=SECTION_IDS,
        )
    assert excinfo.value.code == "invalid_round_result"
    assert prefix_fragment in str(excinfo.value)


def test_canonical_list_preserves_order_without_extra_keys() -> None:
    repairs = [
        {"kind": "add_dependency", "section_id": "2.1", "on": ["1.2", "1.1"]},
        {
            "kind": "add_targets",
            "section_id": "1.2",
            "entries": ["  `src/example.py::build`  ", "`tests/test_example.py`"],
        },
    ]
    canonical = validate_finding_repairs(
        repairs,
        prefix="findings[0]",
        category="gobby-format",
        section_ids=SECTION_IDS,
    )
    assert canonical == [
        {"kind": "add_dependency", "section_id": "2.1", "on": ["1.2", "1.1"]},
        {
            "kind": "add_targets",
            "section_id": "1.2",
            "entries": ["`src/example.py::build`", "`tests/test_example.py`"],
        },
    ]


def test_finding_canonicalizes_repairs() -> None:
    evidence = _evidence()
    repairs = [
        {"kind": "add_targets", "section_id": "1.1", "entries": ["`src/example.py::build`"]},
        {
            "kind": "add_acceptance",
            "section_id": "1.2",
            "items": [{"prose": "Rollback covered", "artifact": "test: `tests/t.py::test_r`"}],
        },
    ]
    findings = validate_plan_review_findings([_finding(repairs=repairs)], evidence=evidence)
    assert findings[0]["repairs"] == repairs

    with pytest.raises(ReviewEvidenceError, match=r"findings\[0\]\.repairs"):
        validate_plan_review_findings(
            [_finding(category="over-engineering", repairs=repairs)],
            evidence=evidence,
        )

    plain = validate_plan_review_findings([_finding()], evidence=evidence)
    assert "repairs" not in plain[0]


def test_finding_item_schema_accepts_repairs() -> None:
    validator = validator_for(FINDING_ITEM_SCHEMA)(FINDING_ITEM_SCHEMA)
    assert "repairs" in FINDING_ITEM_SCHEMA["properties"]
    assert FINDING_ITEM_SCHEMA["additionalProperties"] is False
    assert set(FINDING_ITEM_SCHEMA["properties"]["category"]["enum"]) == FINDING_CATEGORIES
    validator.validate(_finding(repairs=[dict(repair) for repair in _REPAIR_BY_KIND.values()]))
    assert not validator.is_valid(
        _finding(repairs=[{"kind": "rename", "section_id": "1.1", "entries": ["a.py"]}])
    )
    assert not validator.is_valid(_finding(repairs=[]))


def test_rejection_section_projects_repairs() -> None:
    evidence = _evidence()
    findings = validate_plan_review_findings(
        [
            _finding(
                repairs=[
                    {"kind": "add_targets", "section_id": "1.1", "entries": ["`src/a.py::x`"]},
                    {
                        "kind": "add_acceptance",
                        "section_id": "1.2",
                        "items": [
                            {"prose": "Rollback covered", "artifact": "file: `src/a.py`"},
                        ],
                    },
                ]
            ),
            _finding(finding_id="F2", category="unhandled-edge"),
        ],
        evidence=evidence,
    )
    rendered = render_rejection_section(round_number=1, findings=findings, evidence=evidence)
    assert rendered.count("**Repairs:**") == 1
    assert "- add_targets 1.1: `src/a.py::x`" in rendered
    assert "- add_acceptance 1.2: Rollback covered. file: `src/a.py`" in rendered
    assert rendered.index("**Repairs:**") < rendered.index("### F2")
