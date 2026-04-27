from __future__ import annotations

from pathlib import Path

import pytest

from gobby.plans.coverage import (
    COVERS_LABEL_REGEX,
    CoversRecord,
    InvalidCoversLabelError,
    parse_covers_label,
    validate_covers,
)
from gobby.plans.parser import AcceptanceItem, ArtifactKind, Kind, PlanDocument, PlanSection

pytestmark = pytest.mark.unit


def _item(
    item_id: str,
    *,
    artifact_kind: ArtifactKind = ArtifactKind.file,
    artifact_ref: str = "src/gobby/plans/parser.py",
    prose: str | None = None,
) -> AcceptanceItem:
    return AcceptanceItem(
        item_id=item_id,
        prose=prose or f"proves {artifact_ref}",
        artifact_kind=artifact_kind,
        artifact_ref=artifact_ref,
        source_line=5,
    )


def _plan_doc(section_id: str = "A1", *items: AcceptanceItem) -> PlanDocument:
    section = PlanSection(
        section_id=section_id,
        parent_id=None,
        heading_level=2,
        title="Coverage",
        kind=Kind.deliverable,
        acceptance_items=items or (_item(f"{section_id}.1"),),
        deferral=None,
        source_span=(1, 6),
    )
    return PlanDocument(
        plan_id="plan",
        source_path=Path("plan.md"),
        source_hash="hash",
        sections=(section,),
        framing_headings=(),
    )


@pytest.mark.parametrize(
    ("label", "record"),
    [
        (
            "covers:task-12725-lifecycle-dispatch:1.7:1.7.3",
            CoversRecord(
                plan_id="task-12725-lifecycle-dispatch",
                section_id="1.7",
                item_id="1.7.3",
            ),
        ),
        ("covers:plan_1.2:A1:A1.1", CoversRecord("plan_1.2", "A1", "A1.1")),
        ("covers:plan-1:1.7:A1.1", CoversRecord("plan-1", "1.7", "A1.1")),
    ],
)
def test_parse_valid_label(label: str, record: CoversRecord) -> None:
    assert parse_covers_label(label) == record


@pytest.mark.parametrize(
    "label",
    [
        "coverage:task-12725-lifecycle-dispatch:1.7:1.7.3",
        "covers:a:b",
        "covers:task-12725-lifecycle-dispatch:1.7:1.7.3 ",
        "covers:task/12725:A1:A1.1",
        "covers:task:12725:A1:A1.1",
        "covers::A1:A1.1",
    ],
)
def test_parse_rejects_malformed(label: str) -> None:
    with pytest.raises(InvalidCoversLabelError):
        parse_covers_label(label)


def test_regex_plan_id_allowlist_is_sanitize_compatible() -> None:
    assert COVERS_LABEL_REGEX.pattern.startswith("^covers:(?P<plan_id>[A-Za-z0-9._-]+):")
    assert COVERS_LABEL_REGEX.match("covers:abc.DEF_123-45:A1:A1.1") is not None
    assert COVERS_LABEL_REGEX.match("covers:abc/DEF:A1:A1.1") is None


def test_validate_covers_missing_section() -> None:
    result = validate_covers(
        CoversRecord("plan", "A2", "A2.1"),
        "src/gobby/plans/parser.py",
        "#13216",
        _plan_doc("A1", _item("A1.1")),
    )

    assert result.status == "missing_section"


def test_validate_covers_missing_item() -> None:
    result = validate_covers(
        CoversRecord("plan", "A1", "A1.2"),
        "src/gobby/plans/parser.py",
        "#13217",
        _plan_doc("A1", _item("A1.1")),
    )

    assert result.status == "missing_item"


def test_validate_covers_artifact_referenced_by_path() -> None:
    result = validate_covers(
        CoversRecord("plan", "A1", "A1.1"),
        "Verify src/gobby/plans/parser.py handles section parsing.",
        "#leaf",
        _plan_doc("A1", _item("A1.1")),
    )

    assert result.status == "valid"


def test_validate_covers_artifact_referenced_by_symbol_short_name() -> None:
    item = _item(
        "A1.1",
        artifact_kind=ArtifactKind.symbol,
        artifact_ref="gobby.plans.parser.parse_plan",
    )

    result = validate_covers(
        CoversRecord("plan", "A1", "A1.1"),
        "Focused coverage for parse_plan.",
        "#leaf",
        _plan_doc("A1", item),
    )

    assert result.status == "valid"


def test_validate_covers_artifact_referenced_by_test_path_and_name() -> None:
    item = _item(
        "A1.1",
        artifact_kind=ArtifactKind.test,
        artifact_ref="tests/plans/test_parser.py::test_source_hash_is_sha256_of_bytes",
    )

    result = validate_covers(
        CoversRecord("plan", "A1", "A1.1"),
        "Run tests/plans/test_parser.py and test_source_hash_is_sha256_of_bytes.",
        "#leaf",
        _plan_doc("A1", item),
    )

    assert result.status == "valid"


def test_validate_covers_artifact_referenced_by_behavior_substring() -> None:
    item = _item(
        "A1.1",
        artifact_kind=ArtifactKind.behavior,
        artifact_ref="parser ignores fenced fake headings",
        prose="behavior: parser ignores fenced fake headings file: src/gobby/plans/parser.py",
    )

    result = validate_covers(
        CoversRecord("plan", "A1", "A1.1"),
        "Assert PARSER IGNORES FENCED FAKE HEADINGS in src/gobby/plans/parser.py.",
        "#leaf",
        _plan_doc("A1", item),
    )

    assert result.status == "valid"


def test_validate_covers_overbroad_rejected() -> None:
    result = validate_covers(
        CoversRecord("plan", "A1", "A1.1"),
        "Verify the parser and the library generally work.",
        "#13218",
        _plan_doc("A1", _item("A1.1")),
    )

    assert result.status == "artifact_not_referenced"
