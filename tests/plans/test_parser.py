from __future__ import annotations

import hashlib
import inspect
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from gobby.plans import parser
from gobby.plans.coverage import parse_covers_label
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
)
from gobby.plans.parser import parse_plan as _real_parse_plan

pytestmark = pytest.mark.unit


def parse_plan(*args: Any, **kwargs: Any) -> PlanDocument:
    """Test wrapper that defaults to draft mode (manifest validation opt-out).

    Manifest behavior is exercised in tests/plans/test_parser_manifest.py.
    """
    kwargs.setdefault("parse_mode", "draft")
    return _real_parse_plan(*args, **kwargs)


def _write_plan(tmp_path: Path, text: str, name: str = "plan.md") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    return path


def _section_ids(document: PlanDocument) -> set[str]:
    return {section.section_id for section in document.sections}


def _fixture_plan(name: str) -> Path:
    candidates = [
        Path(".gobby/plans") / name,
        Path(".gobby/plans/completed") / name,
    ]
    project_config = Path(".gobby/project.json")
    if project_config.exists():
        data = json.loads(project_config.read_text(encoding="utf-8"))
        parent_project_path = data.get("parent_project_path")
        if parent_project_path:
            parent_plans = Path(parent_project_path) / ".gobby/plans"
            candidates.extend(
                [
                    parent_plans / name,
                    parent_plans / "completed" / name,
                ]
            )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    pytest.skip(f"plan fixture not present in this worktree: {name}")


def test_public_api_exports_parser_contract() -> None:
    assert parser.PlanDocument is PlanDocument
    assert parser.PlanSection is PlanSection
    assert parser.AcceptanceItem is AcceptanceItem
    assert parser.Deferral is Deferral
    assert parser.Kind is Kind
    assert parser.ArtifactKind is ArtifactKind
    assert parser.PlanParseError is PlanParseError
    assert parser.parse_plan is _real_parse_plan
    assert parser.PLAN_HEADING_REGEX == PLAN_HEADING_REGEX


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


@pytest.mark.parametrize("section_kind", ["framing", "verification"])
def test_acceptance_block_under_non_deliverable_fails(
    tmp_path: Path,
    section_kind: str,
) -> None:
    plan = _write_plan(
        tmp_path,
        f"""
        ## A1 Mistyped Deliverable
        `kind: {section_kind}`
        **Acceptance:**
        - A1.1 — file: `src/feature.py`
        """,
    )

    with pytest.raises(PlanParseError) as exc_info:
        parse_plan(plan)

    message = str(exc_info.value)
    assert "section 'A1'" in message
    assert f"kind '{section_kind}'" in message
    assert "must not contain an **Acceptance:** block" in message


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


def test_duplicate_acceptance_item_ids_raise(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## 1
        `kind: deliverable`
        **Acceptance:**
        - 1.1 — first item. file: src/first.py.
        - 1.1 — second item. file: src/second.py.
        """,
    )

    with pytest.raises(PlanParseError, match="duplicate acceptance item ID '1.1'"):
        parse_plan(plan)


@pytest.mark.parametrize(
    "malformed_bullet",
    [
        "- A1.2: second item. file: src/second.py.",
        "- A1.2 – second item. file: src/second.py.",
    ],
    ids=["colon", "en-dash"],
)
def test_acceptance_item_rejects_wrong_separator(tmp_path: Path, malformed_bullet: str) -> None:
    plan = _write_plan(
        tmp_path,
        f"""
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — first item. file: src/first.py.
        {malformed_bullet}
        """,
    )

    with pytest.raises(
        PlanParseError,
        match=r"malformed acceptance item 'A1\.2'.*separator",
    ):
        parse_plan(plan)


def test_acceptance_item_id_must_match_covers_grammar(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## 1.1
        `kind: deliverable`
        **Acceptance:**
        - 1.1.foo — invalid suffix. file: invalid.py
        """,
    )

    with pytest.raises(PlanParseError, match="acceptance item ID '1.1.foo'.*dotted-ID grammar"):
        parse_plan(plan)


@pytest.mark.parametrize(
    ("section_id", "item_id"),
    [
        ("A1", "A1.1"),
        ("AB12", "AB12.3"),
        ("1.1", "1.1.2"),
        ("1.1a", "1.1a.2b"),
        ("A1a", "A1a.2b"),
    ],
)
def test_parsed_acceptance_ids_round_trip_through_covers_labels(
    tmp_path: Path, section_id: str, item_id: str
) -> None:
    plan = _write_plan(
        tmp_path,
        f"""
        ## {section_id}
        `kind: deliverable`
        **Acceptance:**
        - {item_id} — valid item. file: valid.py
        """,
    )

    document = parse_plan(plan)
    parsed_item_id = document.sections[0].acceptance_items[0].item_id
    record = parse_covers_label(f"covers:plan:{section_id}:{parsed_item_id}")

    assert record.section_id == section_id
    assert record.item_id == item_id


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


def test_deferred_with_unlabeled_fence_raises_parse_error(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deferred`
        ```
        deferred prose
        ```
        """,
    )

    with pytest.raises(PlanParseError, match="missing YAML deferral object"):
        parse_plan(plan)


def test_documented_deferral_wrapper_and_scalar_ids_round_trip(tmp_path: Path) -> None:
    canonical_block = """\
deferral:
  task_ref: "#12345"
  reason: "Why this work is outside the current epic."
  owner: "team-or-agent"
  original_acceptance_items:
    - A7.3"""
    repo_root = Path(__file__).resolve().parents[2]
    contract = (repo_root / "docs/contracts/plan-coverage.md").read_text(encoding="utf-8")
    draft_skill = (repo_root / "src/gobby/install/shared/skills/plan-draft/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert canonical_block in contract
    assert canonical_block in draft_skill
    plan = _write_plan(
        tmp_path,
        f"""> **Plan ID:** plan

## A7 Deferred work
`kind: deferred`

```yaml
{canonical_block}
```
""",
    )

    deferral = parse_plan(plan).sections[0].deferral

    assert deferral is not None
    assert deferral.task_ref == "#12345"
    assert deferral.reason == "Why this work is outside the current epic."
    assert deferral.owner == "team-or-agent"
    assert deferral.raw_block == canonical_block
    assert deferral.original_acceptance_items == (
        AcceptanceItem(
            item_id="A7.3",
            prose="A7.3",
            artifact_kind=ArtifactKind.behavior,
            artifact_ref="A7.3",
            source_line=6,
        ),
    )


def test_unwrapped_deferral_block_names_required_wrapper(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        > **Plan ID:** plan

        ## A7 Deferred work
        `kind: deferred`

        ```yaml
        task_ref: "#12345"
        reason: "Why this work is outside the current epic."
        owner: "team-or-agent"
        original_acceptance_items:
          - A7.3
        ```
        """,
    )

    with pytest.raises(PlanParseError, match="required top-level 'deferral:' wrapper"):
        parse_plan(plan)


def test_manifest_with_unlabeled_fence_raises_parse_error(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## M1
        `kind: manifest`
        ```
        manifest prose
        ```
        """,
    )

    with pytest.raises(PlanParseError, match="manifest section missing YAML block"):
        parse_plan(plan)


def test_deferred_object_parsed(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deferred`

        ```yaml
        deferral:
          task_ref: "#999"
          reason: "covered by follow-up"
          owner: "agent"
          original_acceptance_items:
            - A1.1
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
            prose="A1.1",
            artifact_kind=ArtifactKind.behavior,
            artifact_ref="A1.1",
            source_line=4,
        ),
    )


def test_deferred_acceptance_items_require_scalar_ids(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deferred`

        ```yaml
        deferral:
          task_ref: "#999"
          reason: "covered by follow-up"
          owner: "agent"
          original_acceptance_items:
            - item_id: A1.1
        ```
        """,
    )

    with pytest.raises(PlanParseError, match="must be a scalar acceptance-item ID"):
        parse_plan(plan)


def test_deferred_acceptance_item_id_must_match_covers_grammar(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## 1.1
        `kind: deferred`

        ```yaml
        deferral:
          task_ref: "#999"
          reason: "covered by follow-up"
          owner: "agent"
          original_acceptance_items:
            - 1.1.foo
        ```
        """,
    )

    with pytest.raises(PlanParseError, match="acceptance item ID '1.1.foo'.*dotted-ID grammar"):
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


def test_acceptance_list_stops_before_unindented_prose_after_blank(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — real item. file: src/accepted.py.

        Narrative after the list mentions file: src/unrelated.py.
        """,
    )

    item = parse_plan(plan).sections[0].acceptance_items[0]

    assert item.artifact_ref == "src/accepted.py"
    assert "unrelated.py" not in item.prose


def test_trailing_prose_cannot_supply_missing_acceptance_artifact(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — real item without reference.

        Narrative after the list mentions file: src/unrelated.py.
        """,
    )

    with pytest.raises(PlanParseError, match="no artifact reference"):
        parse_plan(plan)


def test_acceptance_item_preserves_indented_continuation_after_blank(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — real item continued below.

          Continued details name file: src/continued.py.
        """,
    )

    item = parse_plan(plan).sections[0].acceptance_items[0]

    assert item.artifact_ref == "src/continued.py"
    assert "Continued details" in item.prose


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


@pytest.mark.parametrize(
    "quoted_ref",
    ["`src/a.py`", '"src/a.py"', "'src/a.py'"],
    ids=["backtick", "double-quote", "single-quote"],
)
def test_quoted_artifact_ref_stops_before_trailing_prose(
    tmp_path: Path,
    quoted_ref: str,
) -> None:
    plan = _write_plan(
        tmp_path,
        f"""
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — file: {quoted_ref} and the tests pass
        """,
    )

    item = parse_plan(plan).sections[0].acceptance_items[0]

    assert item.artifact_kind is ArtifactKind.file
    assert item.artifact_ref == "src/a.py"


@pytest.mark.parametrize(
    ("artifact_prose", "expected_ref"),
    [
        ("file: src/a.py", "src/a.py"),
        ("file: src/a.py symbol: gobby.plans.parser.parse_plan", "src/a.py"),
    ],
    ids=["end-of-line", "next-artifact"],
)
def test_unquoted_artifact_ref_uses_existing_terminators(
    tmp_path: Path,
    artifact_prose: str,
    expected_ref: str,
) -> None:
    plan = _write_plan(
        tmp_path,
        f"""
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — {artifact_prose}
        """,
    )

    item = parse_plan(plan).sections[0].acceptance_items[0]

    assert item.artifact_kind is ArtifactKind.file
    assert item.artifact_ref == expected_ref


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


def test_utf8_bom_is_accepted_and_included_in_source_hash(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    source = "\ufeff**Plan ID:** bom-plan\n\n## A1\n`kind: framing`\n"
    plan.write_bytes(source.encode("utf-8"))

    document = parse_plan(plan)

    assert document.plan_id == "bom-plan"
    assert document.source_lines[0] == "**Plan ID:** bom-plan"
    assert document.source_hash == hashlib.sha256(plan.read_bytes()).hexdigest()


def test_invalid_utf8_raises_plan_parse_error(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_bytes(b"\xff## A1\n`kind: framing`\n")

    with pytest.raises(PlanParseError, match="invalid UTF-8"):
        parse_plan(plan)


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


def test_unclosed_fence_names_opening_line_in_draft_parse_error(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        ## A1
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — real item. file: a.py.

        ```markdown
        ignored fenced content
        ## A2
        `kind: deliverable`
        **Acceptance:**
        - A2.1 — swallowed item. file: b.py.
        """,
    )

    with pytest.raises(
        PlanParseError,
        match=r"line 6: unclosed fence opened at line 6",
    ):
        parse_plan(plan, parse_mode="draft")


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
        deferral:
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
