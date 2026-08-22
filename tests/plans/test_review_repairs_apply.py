"""Pure bytes-in, bytes-out tests for applying accepted plan-review repairs."""

from __future__ import annotations

import pytest

from gobby.plans.review_evidence_io import build_section_manifest, parse_plan_bytes
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_repairs import Repair, apply_repairs
from gobby.plans.symbol_targets import parse_target_line

PLAN_NAME = "review.md"

PLAN = """# Review
**Plan ID:** review

## Overview
`kind: framing`

Context.

## P1 Foundation
`kind: framing`

### 1.1 Implement
`kind: deliverable`

Targets:
- `src/example.py::build`
- `tests/test_example.py`

Body paragraph.

**Acceptance:**

- 1.1.1 - Built. file: `src/example.py`.
- 1.1.2 - Covered. test: `tests/test_example.py::test_build`.

### 1.2 Follow-up (depends: 1.1)
`kind: deliverable`

Target: `src/other.py`

**Acceptance:**

- 1.2.1 — Done. file: `src/other.py`.

## P2 Rollout
`kind: framing`

### 2.1 Ship
`kind: deliverable`

Prose with no inventory block.

**Acceptance:**

- 2.1.1 - Shipped. behavior: "documented" in `docs/ship.md`.
  Continuation of the last item.

  Indented after a blank line.

Trailing paragraph.

## Task Mapping
`kind: framing`

Pending.
"""


def _targets(finding_id: str, section_id: str, *entries: str) -> Repair:
    return Repair(
        finding_id=finding_id,
        kind="add_targets",
        section_id=section_id,
        entries=tuple(entries),
    )


def _dependency(finding_id: str, section_id: str, *on: str) -> Repair:
    return Repair(finding_id=finding_id, kind="add_dependency", section_id=section_id, on=on)


def _acceptance(finding_id: str, section_id: str, *items: tuple[str, str]) -> Repair:
    return Repair(
        finding_id=finding_id,
        kind="add_acceptance",
        section_id=section_id,
        items=items,
    )


def _changed_sections(before: bytes, after: bytes) -> set[str]:
    before_hashes = {
        entry.section_id: entry.section_hash for entry in build_section_manifest(before)
    }
    after_hashes = {entry.section_id: entry.section_hash for entry in build_section_manifest(after)}
    assert set(before_hashes) == set(after_hashes)
    return {key for key, value in after_hashes.items() if before_hashes[key] != value}


def test_add_targets_appends_after_block_tail() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[_targets("F1", "1.1", "`src/consumer.py::use`", "src/example.py::build")],
    )

    text = outcome.updated.decode()
    assert (
        "Targets:\n- `src/example.py::build`\n- `tests/test_example.py`\n- `src/consumer.py::use`\n\nBody paragraph."
        in text
    )
    assert outcome.applied == [
        {
            "finding_id": "F1",
            "kind": "add_targets",
            "section_id": "1.1",
            "added": ["src/consumer.py::use"],
        }
    ]
    assert outcome.skipped == []
    assert _changed_sections(PLAN.encode(), outcome.updated) == {"1.1"}
    assert outcome.diff.startswith("--- ")


def test_add_targets_after_inline_target_header() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[_targets("F1", "1.2", "`src/other_test.py::*` — scope-reason: all handlers")],
    )

    text = outcome.updated.decode()
    assert (
        "Target: `src/other.py`\n- `src/other_test.py::*` — scope-reason: all handlers\n\n**Acceptance:**"
        in text
    )
    assert outcome.applied[0]["added"] == ["src/other_test.py::*"]


def test_add_targets_creates_block_after_kind_line() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[_targets("F1", "2.1", "`src/ship.py`")],
    )

    text = outcome.updated.decode()
    assert (
        "### 2.1 Ship\n`kind: deliverable`\n\nTargets:\n- `src/ship.py`\n\nProse with no inventory block."
        in text
    )
    assert _changed_sections(PLAN.encode(), outcome.updated) == {"2.1"}


def test_add_targets_all_present_is_skipped() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[_targets("F1", "1.1", "`src/example.py::build`")],
    )

    assert outcome.updated == PLAN.encode()
    assert outcome.applied == []
    assert outcome.skipped == [{"finding_id": "F1", "reason": "already_present"}]
    assert outcome.diff == ""


def test_wildcard_conflicting_with_exact_target_is_invalid_repair() -> None:
    with pytest.raises(ReviewEvidenceError) as excinfo:
        apply_repairs(
            PLAN.encode(),
            plan_name=PLAN_NAME,
            repairs=[_targets("F1", "1.1", "`src/example.py::* — scope-reason: whole file`")],
        )
    assert excinfo.value.code == "invalid_repair"


def test_add_dependency_rebuilds_heading_and_dedupes_phase() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[
            _dependency("F1", "2.1", "P1", "1.1"),
            _dependency("F2", "1.2", "1.1"),
        ],
    )

    text = outcome.updated.decode()
    assert "### 2.1 Ship (depends: P1)\n" in text
    assert "### 1.2 Follow-up (depends: 1.1)\n" in text
    assert outcome.applied == [
        {"finding_id": "F1", "kind": "add_dependency", "section_id": "2.1", "added": ["P1"]}
    ]
    assert outcome.skipped == [{"finding_id": "F2", "reason": "already_present"}]
    assert _changed_sections(PLAN.encode(), outcome.updated) == {"2.1"}


def test_add_dependency_merges_existing_heading_list() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[_dependency("F1", "1.2", "P2", "1.1")],
    )

    assert "### 1.2 Follow-up (depends: 1.1, P2)\n" in outcome.updated.decode()
    assert outcome.applied[0]["added"] == ["P2"]


def test_add_acceptance_after_multiline_last_item_copies_separator() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[
            _acceptance("F1", "2.1", ("Rollback proven", "test: `tests/test_ship.py::test_rb`")),
            _acceptance("F2", "1.2", ("Logged", "file: `src/other.py`")),
        ],
    )

    text = outcome.updated.decode()
    assert (
        "  Indented after a blank line.\n"
        "- 2.1.2 - Rollback proven. test: `tests/test_ship.py::test_rb`.\n"
        "\nTrailing paragraph."
    ) in text
    assert (
        "- 1.2.1 — Done. file: `src/other.py`.\n- 1.2.2 — Logged. file: `src/other.py`.\n" in text
    )
    document = parse_plan_bytes(PLAN_NAME, outcome.updated)
    items = {item.item_id for section in document.sections for item in section.acceptance_items}
    assert {"2.1.2", "1.2.2"} <= items
    assert _changed_sections(PLAN.encode(), outcome.updated) == {"2.1", "1.2"}


def test_add_acceptance_existing_item_is_already_present() -> None:
    outcome = apply_repairs(
        PLAN.encode(),
        plan_name=PLAN_NAME,
        repairs=[_acceptance("F1", "1.1", ("Built", "file: `src/example.py`"))],
    )

    assert outcome.updated == PLAN.encode()
    assert outcome.skipped == [{"finding_id": "F1", "reason": "already_present"}]


def test_eof_without_newline_round_trips() -> None:
    plan = PLAN.rstrip("\n").encode()
    outcome = apply_repairs(
        plan,
        plan_name=PLAN_NAME,
        repairs=[_targets("F1", "1.1", "`src/consumer.py`")],
    )
    assert not outcome.updated.endswith(b"\n")
    assert outcome.updated.decode().endswith("Pending.")


def test_two_sections_change_only_their_hashes() -> None:
    repairs = [
        _targets("F1", "1.1", "`src/consumer.py::use`"),
        _acceptance("F1", "1.1", ("Consumer updated", "file: `src/consumer.py`")),
        _dependency("F2", "2.1", "1.1"),
    ]
    outcome = apply_repairs(PLAN.encode(), plan_name=PLAN_NAME, repairs=repairs)

    assert _changed_sections(PLAN.encode(), outcome.updated) == {"1.1", "2.1"}
    assert [entry["finding_id"] for entry in outcome.applied] == ["F1", "F1", "F2"]
    parse_plan_bytes(PLAN_NAME, outcome.updated)

    again = apply_repairs(outcome.updated, plan_name=PLAN_NAME, repairs=repairs)
    assert again.updated == outcome.updated
    assert again.applied == []
    assert {entry["reason"] for entry in again.skipped} == {"already_present"}


def test_crlf_is_unsupported_plan_text() -> None:
    with pytest.raises(ReviewEvidenceError) as excinfo:
        apply_repairs(
            PLAN.replace("\n", "\r\n").encode(),
            plan_name=PLAN_NAME,
            repairs=[_targets("F1", "1.1", "`src/consumer.py`")],
        )
    assert excinfo.value.code == "unsupported_plan_text"


def test_missing_or_framing_section_is_rejected() -> None:
    with pytest.raises(ReviewEvidenceError) as missing:
        apply_repairs(PLAN.encode(), plan_name=PLAN_NAME, repairs=[_targets("F1", "9.9", "`a.py`")])
    assert missing.value.code == "repair_section_missing"
    with pytest.raises(ReviewEvidenceError) as framing:
        apply_repairs(PLAN.encode(), plan_name=PLAN_NAME, repairs=[_targets("F1", "P1", "`a.py`")])
    assert framing.value.code == "repair_section_not_deliverable"


def test_post_surgery_parse_failure_is_invalid_repair() -> None:
    with pytest.raises(ReviewEvidenceError) as excinfo:
        apply_repairs(
            PLAN.encode(),
            plan_name=PLAN_NAME,
            repairs=[
                _acceptance("F1", "1.1", ("Broken", "file: `src/x.py`\n- 9.9.9 - x. file: `y`"))
            ],
        )
    assert excinfo.value.code == "invalid_repair"


def test_unparseable_plan_is_invalid_plan() -> None:
    with pytest.raises(ReviewEvidenceError) as excinfo:
        apply_repairs(b"# Broken\n\n```\nunclosed", plan_name=PLAN_NAME, repairs=[])
    assert excinfo.value.code == "invalid_plan"


def test_add_targets_entries_parse_with_public_helper() -> None:
    targets, issues = parse_target_line("- `src/example.py::build`", "1.1")
    assert issues == []
    assert [target.reference for target in targets] == ["src/example.py::build"]
    assert parse_plan_bytes(PLAN_NAME, PLAN.encode()).plan_id == "review"
