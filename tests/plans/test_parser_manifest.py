"""Tests for plan parser manifest handling."""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path
from typing import get_args

import pytest

from gobby.plans.parser import (
    Kind,
    ManifestEntry,
    ParseMode,
    PlanKind,
    PlanParseError,
    parse_plan,
)

pytestmark = pytest.mark.unit


def _write_plan(tmp_path: Path, text: str, name: str = "plan.md") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip("\n").rstrip() + "\n", encoding="utf-8")
    return path


def _plan_with_manifest(
    tmp_path: Path,
    *,
    deliverables: str = "",
    manifest_yaml: str = "",
    plan_id: str = "test-plan",
    name: str = "plan.md",
) -> Path:
    body = f"> **Plan ID:** {plan_id}\n\n{deliverables.rstrip()}\n"
    if manifest_yaml:
        body += "\n## M1 Task Manifest\n`kind: manifest`\n\n```yaml\n"
        body += manifest_yaml.rstrip() + "\n```\n"
    return _write_plan(tmp_path, body, name)


_MINIMAL_DELIVERABLE = """
## A1 Section
`kind: deliverable`
**Acceptance:**
- A1.1 — file: `foo.py`
"""

_MINIMAL_MANIFEST = """
- title: "Build foo"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "foo.py exists"
  labels:
    - "covers:test-plan:A1:A1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A1"
"""

_TWO_DELIVERABLES = """
## A1 Section
`kind: deliverable`
**Acceptance:**
- A1.1 — file: `foo.py`

## A2 Other Section
`kind: deliverable`
**Acceptance:**
- A2.1 — file: `bar.py`
"""

_LINKED_MANIFEST = """
- title: "Build foo"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: ["A2"]
  validation_criteria: "foo.py exists"
  labels:
    - "covers:test-plan:A1:A1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A1"
- title: "Build bar"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "bar.py exists"
  labels:
    - "covers:test-plan:A2:A2.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A2"
"""


def test_parse_mode_signature_accepts_three_values() -> None:
    """2.21.3 — parse_plan accepts parse_mode parameter with values draft/expansion/strict."""
    signature = inspect.signature(parse_plan)
    parameter = signature.parameters["parse_mode"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == "strict"
    assert set(get_args(ParseMode)) == {"draft", "expansion", "strict"}
    override = signature.parameters["plan_id_override"]
    assert override.kind is inspect.Parameter.KEYWORD_ONLY
    assert override.default is None


def test_draft_tolerates_missing_manifest_but_validates_present_one(tmp_path: Path) -> None:
    """2.21.3a — draft mode passes when manifest absent; validates schema when present."""
    plan_no_manifest = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml="",
        name="no_manifest.md",
    )
    document = parse_plan(plan_no_manifest, parse_mode="draft")
    assert document.manifest_entries == ()

    malformed_yaml = """
- title: "Build foo"
  category: code
  implementation_domain: backend
"""
    plan_malformed = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=malformed_yaml,
        name="malformed.md",
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan_malformed, parse_mode="draft")
    assert "manifest" in str(excinfo.value).lower()


def test_expansion_rejects_missing_manifest(tmp_path: Path) -> None:
    """2.21.3b — expansion mode raises PlanParseError on missing manifest."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml="",
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="expansion")
    assert "missing manifest" in str(excinfo.value).lower()


def test_strict_default_rejects_missing_manifest(tmp_path: Path) -> None:
    """2.21.3c — strict (default) mode raises PlanParseError on missing manifest."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml="",
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan)
    assert "missing manifest" in str(excinfo.value).lower()

    with pytest.raises(PlanParseError):
        parse_plan(plan, parse_mode="strict")


def test_manifest_invariants_enforced_in_all_modes(tmp_path: Path) -> None:
    """2.21.3d — when manifest present, schema/1:1/covers/orphan invariants enforced in all modes."""
    orphan_yaml = """
- title: "Build foo"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "foo.py exists"
  labels:
    - "covers:test-plan:A1:A1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A1"
- title: "Orphan entry"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "bar.py exists"
  labels:
    - "covers:test-plan:Z9:Z9.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "Z9"
"""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=orphan_yaml,
    )
    for mode in ("draft", "expansion", "strict"):
        with pytest.raises(PlanParseError) as excinfo:
            parse_plan(plan, parse_mode=mode)
        assert "Z9" in str(excinfo.value) or "orphan" in str(excinfo.value).lower()


def test_well_formed_manifest_parses_in_strict_mode(tmp_path: Path) -> None:
    """Parse a complete manifest entry in strict mode."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=_MINIMAL_MANIFEST,
    )
    document = parse_plan(plan)
    assert len(document.manifest_entries) == 1
    entry = document.manifest_entries[0]
    assert isinstance(entry, ManifestEntry)
    assert entry.title == "Build foo"
    assert entry.category == "code"
    assert entry.implementation_domain == "backend"
    assert entry.task_type == "feature"
    assert entry.source_section == "A1"
    assert entry.tdd is True
    assert entry.labels == ("covers:test-plan:A1:A1.1",)


def test_manifest_kind_line_allows_plain_and_spaced_backticked_forms(tmp_path: Path) -> None:
    """Manifest detection accepts plain kind lines and backticked kind lines with spaces."""
    for index, kind_line in enumerate(("kind: manifest", "` kind: manifest `"), start=1):
        body = (
            f"> **Plan ID:** test-plan\n\n"
            f"{_MINIMAL_DELIVERABLE.rstrip()}\n\n"
            f"## M1 Task Manifest\n{kind_line}\n\n"
            f"```yaml\n{_MINIMAL_MANIFEST.rstrip()}\n```\n"
        )
        plan = _write_plan(
            tmp_path,
            body,
            name=f"manifest-kind-{index}.md",
        )

        document = parse_plan(plan)

        assert len(document.manifest_entries) == 1


def test_manifest_rejects_manual_category(tmp_path: Path) -> None:
    """Reject manual as an automated expansion manifest category."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=_MINIMAL_MANIFEST.replace("category: code", "category: manual").replace(
            "tdd: true", "tdd: false"
        ),
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="expansion")

    message = str(excinfo.value)
    assert "unsupported category 'manual'" in message
    assert "development-forward categories" in message


def test_code_manifest_entry_requires_implementation_domain(tmp_path: Path) -> None:
    """Code manifest entries must route through an explicit implementation domain."""
    manifest_yaml = _MINIMAL_MANIFEST.replace("  implementation_domain: backend\n", "")
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=manifest_yaml,
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="expansion")

    message = str(excinfo.value)
    assert "category 'code' requires implementation_domain" in message


def test_plan_id_override_validates_covers_labels(tmp_path: Path) -> None:
    """Use the plan-id override when validating covers labels."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=_MINIMAL_MANIFEST.replace("covers:test-plan", "covers:12761"),
        plan_id="12761",
        name="task-12761-demo.md",
    )

    document = parse_plan(plan, parse_mode="expansion", plan_id_override="12761")

    assert document.plan_id == "12761"
    assert document.manifest_entries[0].labels == ("covers:12761:A1:A1.1",)


def test_plan_id_override_rejects_embedded_mismatch(tmp_path: Path) -> None:
    """Reject a plan-id override that conflicts with the document header."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=_MINIMAL_MANIFEST,
        plan_id="test-plan",
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="expansion", plan_id_override="other-plan")

    assert "does not match override" in str(excinfo.value)


def test_task_filename_fallback_sets_plan_id(tmp_path: Path) -> None:
    """Infer plan_id from a task-prefixed filename when no header exists."""
    plan = _write_plan(
        tmp_path,
        """
        ## A1 Section
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — file: `foo.py`

        ## M1 Task Manifest
        `kind: manifest`

        ```yaml
        - title: "Build foo"
          category: code
          implementation_domain: backend
          task_type: feature
          depends_on: []
          validation_criteria: "foo.py exists"
          labels:
            - "covers:12761:A1:A1.1"
          assigned_agent: backend-developer
          tdd: true
          source_section: "A1"
        ```
        """,
        name="task-12761-demo.md",
    )

    document = parse_plan(plan, parse_mode="expansion")

    assert document.plan_id == "12761"


@pytest.mark.parametrize("parse_mode", get_args(ParseMode))
@pytest.mark.parametrize("missing_dependency", ["P0", "99.99"])
def test_manifest_depends_on_must_reference_manifest_entry_source_section(
    tmp_path: Path,
    parse_mode: ParseMode,
    missing_dependency: str,
) -> None:
    """Reject manifest dependencies that do not point at manifest source sections."""
    manifest_yaml = _LINKED_MANIFEST.replace(
        'depends_on: ["A2"]',
        f'depends_on: ["{missing_dependency}"]',
    )
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_TWO_DELIVERABLES,
        manifest_yaml=manifest_yaml,
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode=parse_mode)

    message = str(excinfo.value)
    assert "source_section='A1'" in message
    assert f"depends on '{missing_dependency}'" in message
    assert "has no manifest entry" in message


@pytest.mark.parametrize("parse_mode", get_args(ParseMode))
def test_manifest_depends_on_resolves_to_manifest_entry_source_section(
    tmp_path: Path,
    parse_mode: ParseMode,
) -> None:
    """Preserve valid manifest dependency source-section references."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_TWO_DELIVERABLES,
        manifest_yaml=_LINKED_MANIFEST,
    )

    document = parse_plan(plan, parse_mode=parse_mode)

    entries_by_section = {entry.source_section: entry for entry in document.manifest_entries}
    assert entries_by_section["A1"].depends_on == ("A2",)
    assert entries_by_section["A2"].depends_on == ()


def test_test_category_with_tdd_true_fails(tmp_path: Path) -> None:
    """Reject test-category manifest entries that still request TDD expansion."""
    manifest_yaml = _MINIMAL_MANIFEST.replace("category: code", "category: test")
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=manifest_yaml,
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan)

    message = str(excinfo.value)
    assert "tdd: true" in message
    assert "category 'test'" in message


def test_test_category_with_tdd_false_passes(tmp_path: Path) -> None:
    """Allow test-category manifest entries when TDD wrapping is disabled."""
    manifest_yaml = _MINIMAL_MANIFEST.replace("category: code", "category: test").replace(
        "tdd: true",
        "tdd: false",
    )
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=manifest_yaml,
    )

    document = parse_plan(plan)

    entry = document.manifest_entries[0]
    assert entry.category == "test"
    assert entry.tdd is False


def test_code_category_with_tdd_false_passes(tmp_path: Path) -> None:
    """Allow code-category manifest entries to opt out of TDD wrapping."""
    manifest_yaml = _MINIMAL_MANIFEST.replace("tdd: true", "tdd: false")
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=manifest_yaml,
    )

    document = parse_plan(plan)

    entry = document.manifest_entries[0]
    assert entry.category == "code"
    assert entry.tdd is False


def test_manifest_section_kind_recognized(tmp_path: Path) -> None:
    """Classify task manifest sections as Kind.manifest."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=_MINIMAL_MANIFEST,
    )
    document = parse_plan(plan)
    manifest_sections = [s for s in document.sections if s.kind is Kind.manifest]
    assert len(manifest_sections) == 1
    assert manifest_sections[0].section_id == "M1"


def test_deliverable_with_no_manifest_entry_fails_one_to_one(tmp_path: Path) -> None:
    deliverables = _MINIMAL_DELIVERABLE + textwrap.dedent(
        """
        ## A2 Other Section
        `kind: deliverable`
        **Acceptance:**
        - A2.1 — file: `bar.py`
        """
    )
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=deliverables,
        manifest_yaml=_MINIMAL_MANIFEST,
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan)
    message = str(excinfo.value)
    assert "A2" in message


def test_expansion_mode_rejects_deliverable_without_manifest_entry(tmp_path: Path) -> None:
    deliverables = _MINIMAL_DELIVERABLE + textwrap.dedent(
        """
        ## A2 Other Section
        `kind: deliverable`
        **Acceptance:**
        - A2.1 — file: `bar.py`
        """
    )
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=deliverables,
        manifest_yaml=_MINIMAL_MANIFEST,
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="expansion")

    message = str(excinfo.value)
    assert "A2" in message
    assert "manifest" in message


def test_duplicate_manifest_entry_for_one_deliverable_fails(tmp_path: Path) -> None:
    duplicated_yaml = _MINIMAL_MANIFEST + textwrap.dedent(
        """
        - title: "Build foo again"
          category: code
          implementation_domain: backend
          task_type: feature
          depends_on: []
          validation_criteria: "foo.py exists also"
          labels:
            - "covers:test-plan:A1:A1.1"
          assigned_agent: backend-developer
          tdd: true
          source_section: "A1"
        """
    )
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=duplicated_yaml,
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan)
    message = str(excinfo.value)
    assert "A1" in message


def test_missing_covers_label_for_acceptance_item_fails(tmp_path: Path) -> None:
    deliverables = textwrap.dedent(
        """
        ## A1 Section
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — file: `foo.py`
        - A1.2 — file: `bar.py`
        """
    )
    manifest_yaml = """
- title: "Build foo"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "foo.py exists"
  labels:
    - "covers:test-plan:A1:A1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A1"
"""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=deliverables,
        manifest_yaml=manifest_yaml,
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan)
    message = str(excinfo.value)
    assert "A1.2" in message


def test_more_than_one_manifest_section_fails(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        > **Plan ID:** test-plan

        ## A1 Section
        `kind: deliverable`
        **Acceptance:**
        - A1.1 — file: `foo.py`

        ## M1 Task Manifest
        `kind: manifest`

        ```yaml
        - title: "Build foo"
          category: code
          implementation_domain: backend
          task_type: feature
          depends_on: []
          validation_criteria: "foo.py exists"
          labels:
            - "covers:test-plan:A1:A1.1"
          assigned_agent: backend-developer
          tdd: true
          source_section: "A1"
        ```

        ## M2 Task Manifest
        `kind: manifest`

        ```yaml
        - title: "Other"
          category: code
          implementation_domain: backend
          task_type: feature
          depends_on: []
          validation_criteria: "x"
          labels:
            - "covers:test-plan:A1:A1.1"
          assigned_agent: backend-developer
          tdd: true
          source_section: "A1"
        ```
        """
    )
    plan = _write_plan(tmp_path, body)
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="draft")
    message = str(excinfo.value).lower()
    assert "manifest" in message


def test_no_deliverables_strict_still_requires_manifest(tmp_path: Path) -> None:
    """Per §2.21: every implementation plan needs a manifest, even with no deliverables."""
    body = textwrap.dedent(
        """
        > **Plan ID:** test-plan

        ## A1 Framing
        `kind: framing`

        Some framing prose.
        """
    )
    plan = _write_plan(tmp_path, body)
    with pytest.raises(PlanParseError):
        parse_plan(plan)


def test_strategy_plan_does_not_require_manifest(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        ## A1 Framing
        `kind: framing`

        Strategy prose.

        ## A2 More Framing
        `kind: framing`

        Strategy continues.
        """
    )
    plan = _write_plan(tmp_path, body)
    document = parse_plan(plan, plan_kind=PlanKind.strategy)
    assert document.manifest_entries == ()


def test_manifest_entry_schema_missing_required_field_fails(tmp_path: Path) -> None:
    schema_bad_yaml = """
- title: "Build foo"
  category: code
  implementation_domain: backend
"""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=schema_bad_yaml,
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="draft")
    message = str(excinfo.value).lower()
    assert "manifest" in message


def test_manifest_entry_with_unknown_source_section_fails(tmp_path: Path) -> None:
    """Orphan entry whose source_section does not resolve to any deliverable."""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables="",
        manifest_yaml=_MINIMAL_MANIFEST,
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="draft")
    message = str(excinfo.value)
    assert "A1" in message


def test_covers_label_with_wrong_plan_id_fails(tmp_path: Path) -> None:
    manifest_yaml = """
- title: "Build foo"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "foo.py exists"
  labels:
    - "covers:wrong-plan:A1:A1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A1"
"""
    plan = _plan_with_manifest(
        tmp_path,
        deliverables=_MINIMAL_DELIVERABLE,
        manifest_yaml=manifest_yaml,
    )
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan, parse_mode="draft")
    message = str(excinfo.value)
    assert "A1.1" in message or "wrong-plan" in message
