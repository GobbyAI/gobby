from __future__ import annotations

import hashlib
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from gobby.plans import parser
from gobby.plans.parser import (
    PLAN_HEADING_REGEX,
    AcceptanceItem,
    ArtifactKind,
    Deferral,
    Kind,
    PlanDocument,
    PlanKind,
    PlanParseError,
    PlanSection,
    parse_plan,
)

pytestmark = pytest.mark.unit


def _write_plan(tmp_path: Path, text: str, name: str = "plan.md") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    return path


def _section_ids(document: PlanDocument) -> set[str]:
    return {section.section_id for section in document.sections}


def _fixture_plan(name: str) -> Path:
    candidates = [Path(".gobby/plans") / name]
    project_config = Path(".gobby/project.json")
    if project_config.exists():
        data = json.loads(project_config.read_text(encoding="utf-8"))
        parent_project_path = data.get("parent_project_path")
        if parent_project_path:
            candidates.append(Path(parent_project_path) / ".gobby/plans" / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    pytest.skip(f"plan fixture not present in this worktree: {name}")


def test_public_api_exports_parser_contract() -> None:
    assert PlanDocument
    assert PlanSection
    assert AcceptanceItem
    assert Deferral
    assert Kind
    assert ArtifactKind
    assert PlanParseError
    assert parse_plan
    assert PLAN_HEADING_REGEX


def test_parses_task_13173_recovery() -> None:
    path = _fixture_plan("task-13173-lifecycle-dispatch-recovery.md")

    document = parse_plan(path, plan_kind=PlanKind.strategy)

    expected_ids = {
        *(f"A{index}" for index in range(1, 11)),
        *(f"D0.{index}" for index in range(1, 10)),
        *(f"B{index}" for index in range(1, 6)),
        *(f"C{index}" for index in range(1, 7)),
        *(f"D{index}" for index in range(1, 9)),
        *(f"F{index}" for index in range(1, 5)),
    }
    assert expected_ids <= _section_ids(document)
    for section in document.sections:
        assert section.kind is Kind.framing
        assert section.acceptance_items == ()
    assert any("Context" in raw for _, raw, _ in document.framing_headings)
    assert any("Verification" in raw for _, raw, _ in document.framing_headings)


def test_parses_task_12725_lifecycle_dispatch() -> None:
    path = _fixture_plan("task-12725-lifecycle-dispatch-rev1.md")

    document = parse_plan(path)

    expected_ids = {
        "1.3",
        "1.3a",
        "1.4",
        "1.5",
        "1.6",
        "1.7",
        "1.8",
        "1.9",
        "1.10",
        "2.8",
        "2.8b",
        "2.9",
        "2.10",
        "3.2",
    }
    assert expected_ids <= _section_ids(document)


def test_parses_self() -> None:
    path = _fixture_plan("task-13175-plan-coverage-contract.md")

    document = parse_plan(path)

    expected_ids = {f"A{index}" for index in range(13)}
    assert _section_ids(document) == expected_ids
    for section in document.sections:
        if section.kind is Kind.deliverable:
            assert section.acceptance_items
    assert document.source_hash == hashlib.sha256(path.read_bytes()).hexdigest()


def test_bare_and_titled_headings(tmp_path: Path) -> None:
    bare = _write_plan(
        tmp_path,
        """
        ### 1.1a
        `kind: framing`
        """,
        "bare.md",
    )
    titled = _write_plan(
        tmp_path,
        """
        ### 1.1a Title here
        `kind: framing`
        """,
        "titled.md",
    )

    assert parse_plan(bare).sections[0].section_id == "1.1a"
    assert parse_plan(titled).sections[0].section_id == "1.1a"


def test_alpha_and_numeric_ids(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
        ## A10
        `kind: framing`
        ### D0.1
        `kind: framing`
        ### B5
        `kind: framing`
        ### 1.1a
        `kind: framing`
        ### 2.8b
        `kind: framing`
        """,
    )

    assert _section_ids(parse_plan(plan)) == {"A1", "A10", "D0.1", "B5", "1.1a", "2.8b"}


def test_framing_without_id_is_recorded(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## Phase A - Free Form
        `kind: framing`
        """,
    )

    document = parse_plan(plan)

    assert document.sections == ()
    assert document.framing_headings == ((1, "## Phase A - Free Form", 2),)


def test_framing_without_id_no_kind_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## Phase A - Free Form
        """,
    )

    with pytest.raises(PlanParseError):
        parse_plan(plan)


def test_duplicate_section_id_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
        ## A1 Again
        `kind: framing`
        """,
    )

    with pytest.raises(PlanParseError, match="duplicate section ID"):
        parse_plan(plan)


def test_deliverable_without_acceptance_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        """,
    )

    with pytest.raises(PlanParseError, match="missing \\*\\*Acceptance:"):
        parse_plan(plan)


def test_acceptance_item_id_must_prefix_section(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A2.1 \u2014 wrong section. file: wrong.py.
        """,
    )

    with pytest.raises(PlanParseError, match="does not belong"):
        parse_plan(plan)


def test_deferred_without_object_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deferred`
        """,
    )

    with pytest.raises(PlanParseError, match="missing YAML deferral object"):
        parse_plan(plan)


def test_deferred_object_parsed(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deferred`

        ```yaml
        task_ref: "#999"
        reason: "covered by follow-up"
        owner: "agent"
        original_acceptance_items:
          - item_id: A1.1
            prose: "implement later"
            artifact_kind: file
            artifact_ref: "src/later.py"
        ```
        """,
    )

    document = parse_plan(plan)
    deferral = document.sections[0].deferral

    assert deferral is not None
    assert deferral.task_ref == "#999"
    assert deferral.reason == "covered by follow-up"
    assert deferral.owner == "agent"
    assert deferral.original_acceptance_items == (
        AcceptanceItem(
            item_id="A1.1",
            prose="implement later",
            artifact_kind=ArtifactKind.file,
            artifact_ref="src/later.py",
            source_line=4,
        ),
    )


def test_invalid_deferred_artifact_kind_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deferred`

        ```yaml
        task_ref: "#999"
        reason: "covered by follow-up"
        owner: "agent"
        original_acceptance_items:
          - item_id: A1.1
            prose: "implement later"
            artifact_kind: package
            artifact_ref: "src/later.py"
        ```
        """,
    )

    with pytest.raises(PlanParseError, match="invalid artifact_kind"):
        parse_plan(plan)


def test_acceptance_item_without_artifact_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 \u2014 real item without reference.
        """,
    )

    with pytest.raises(PlanParseError, match="no artifact reference"):
        parse_plan(plan)


def test_acceptance_item_with_multiple_artifacts_uses_first(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 \u2014 real item. test: tests/first.py::test_one file: src/second.py.
        """,
    )

    item = parse_plan(plan).sections[0].acceptance_items[0]

    assert item.artifact_kind is ArtifactKind.test
    assert item.artifact_ref == "tests/first.py::test_one"


def test_strategy_kind_permissive_no_raise_on_narrative_headings(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## Context
        prose
        """,
    )

    document = parse_plan(plan, plan_kind=PlanKind.strategy)

    assert document.framing_headings == ((1, "## Context", 2),)
    with pytest.raises(PlanParseError):
        parse_plan(plan, plan_kind=PlanKind.implementation)


def test_strategy_kind_permissive_canonical_heading_no_kind(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ### A1. Plan format spec (typed grammar)
        prose
        """,
    )

    document = parse_plan(plan, plan_kind=PlanKind.strategy)

    assert document.sections[0].section_id == "A1"
    assert document.sections[0].kind is Kind.framing
    assert document.sections[0].acceptance_items == ()
    with pytest.raises(PlanParseError, match="missing kind"):
        parse_plan(plan, plan_kind=PlanKind.implementation)


def test_source_hash_is_sha256_of_bytes(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
        """,
    )

    assert parse_plan(plan).source_hash == hashlib.sha256(plan.read_bytes()).hexdigest()


def test_source_span_is_inclusive_1_indexed(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        # Title
        ## A1 First
        `kind: framing`

        Body line.
        ## A2 Second
        `kind: framing`
        """,
    )

    first, second = parse_plan(plan).sections

    assert first.source_span == (2, 5)
    assert second.source_span == (6, 7)


def test_parser_module_does_not_import_storage() -> None:
    assert "gobby.storage" not in inspect.getsource(parser)


def test_fenced_headings_are_masked(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 \u2014 real item. file: a.py.

        ```markdown
        ## A2
        kind: deliverable
        ```
        """,
    )

    document = parse_plan(plan)

    assert _section_ids(document) == {"A1"}
    assert len(document.sections[0].acceptance_items) == 1


def test_fenced_plan_id_is_masked(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ```markdown
        **Plan ID:** example-plan
        ```

        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 \u2014 real item. file: a.py.
        """,
    )

    document = parse_plan(plan)

    assert document.plan_id is None


def test_fenced_acceptance_bullets_are_masked(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 \u2014 real item. file: a.py.

        ```
        **Acceptance:**
        - A1.2 \u2014 fake. file: b.py.
        ```
        """,
    )

    items = parse_plan(plan).sections[0].acceptance_items

    assert len(items) == 1
    assert {item.item_id for item in items} == {"A1.1"}


def test_fenced_deferral_yaml_outside_deferred_is_ignored(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 \u2014 real item. file: a.py.

        ```yaml
        task_ref: "#999"
        reason: "ignore me"
        owner: "agent"
        original_acceptance_items: []
        ```
        """,
    )

    assert parse_plan(plan).sections[0].deferral is None


def test_tilde_fence_also_masks(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`

        ~~~markdown
        ## A2
        `kind: framing`
        ~~~
        """,
    )

    assert _section_ids(parse_plan(plan)) == {"A1"}


def test_fence_closes_with_longer_delimiter(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
        ```
        ## A2
        `kind: framing`
        ````
        ## A3
        `kind: framing`
        """,
    )

    assert _section_ids(parse_plan(plan)) == {"A1", "A3"}


def test_shorter_inner_fence_does_not_close(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
        ````
        ## A2
        `kind: framing`
        ```
        ## A3
        `kind: framing`
        ````
        ## A4
        `kind: framing`
        """,
    )

    assert _section_ids(parse_plan(plan)) == {"A1", "A4"}


def test_different_fence_char_does_not_close(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
        ```
        ## A2
        `kind: framing`
        ~~~
        ## A3
        `kind: framing`
        ```
        ## A4
        `kind: framing`
        """,
    )

    assert _section_ids(parse_plan(plan)) == {"A1", "A4"}


def test_indented_fence_up_to_3_spaces(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: framing`
           ```
        ## A2
        `kind: framing`
           ```
            ```
        ## A3
        `kind: framing`
            ```
        """,
    )

    assert _section_ids(parse_plan(plan)) == {"A1", "A3"}
