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
  task_type: feature
  depends_on: []
  validation_criteria: "foo.py exists"
  labels:
    - "covers:test-plan:A1:A1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "A1"
"""


def test_parse_mode_signature_accepts_three_values() -> None:
    """2.21.3 — parse_plan accepts parse_mode parameter with values draft/expansion/strict."""
    signature = inspect.signature(parse_plan)
    parameter = signature.parameters["parse_mode"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == "strict"
    assert set(get_args(ParseMode)) == {"draft", "expansion", "strict"}


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
    assert entry.task_type == "feature"
    assert entry.source_section == "A1"
    assert entry.tdd is True
    assert entry.labels == ("covers:test-plan:A1:A1.1",)


def test_manifest_section_kind_recognized(tmp_path: Path) -> None:
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


def test_duplicate_manifest_entry_for_one_deliverable_fails(tmp_path: Path) -> None:
    duplicated_yaml = _MINIMAL_MANIFEST + textwrap.dedent(
        """
        - title: "Build foo again"
          category: code
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
