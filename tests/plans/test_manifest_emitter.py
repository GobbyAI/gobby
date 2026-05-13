"""Tests for §2.21a manifest emitter library."""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path
from typing import get_args

import pytest

from gobby.plans.manifest_emitter import EmitOutcome, emit_stub_manifest
from gobby.plans.parser import (
    MISSING_PLAN_ID_SENTINEL,
    PlanKind,
    PlanParseError,
    parse_plan,
    resolve_plan_id,
)

pytestmark = pytest.mark.unit


def _write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _plan_two_deliverables(tmp_path: Path, name: str = "plan.md") -> Path:
    return _write(
        tmp_path / name,
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Foundation [category: code]
        `kind: deliverable`

        Implement the first behavior.

        **Acceptance:**
        - 1.1.1 — Foundation exists. file: `src/foundation.py`

        ### 1.2 Docs [category: docs]
        `kind: deliverable`

        Document it.

        **Acceptance:**
        - 1.2.1 — Doc exists. file: `docs/foundation.md`
        """,
    )


def _valid_manifest_yaml(plan_id: str = "demo-plan") -> str:
    return textwrap.dedent(
        f"""
        - title: "Foundation"
          category: code
          task_type: feature
          depends_on: []
          validation_criteria: "src/foundation.py"
          labels:
            - "covers:{plan_id}:1.1:1.1.1"
          assigned_agent: backend-developer
          tdd: true
          source_section: "1.1"
        - title: "Docs"
          category: docs
          task_type: feature
          depends_on: []
          validation_criteria: "docs/foundation.md"
          labels:
            - "covers:{plan_id}:1.2:1.2.1"
          assigned_agent: backend-developer
          tdd: false
          source_section: "1.2"
        """
    ).strip()


def _plan_with_manifest_yaml(tmp_path: Path, manifest_yaml: str, name: str = "plan.md") -> Path:
    deliverables = textwrap.dedent(
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Foundation [category: code]
        `kind: deliverable`

        Body.

        **Acceptance:**
        - 1.1.1 — Foundation exists. file: `src/foundation.py`

        ### 1.2 Docs [category: docs]
        `kind: deliverable`

        Body.

        **Acceptance:**
        - 1.2.1 — Doc exists. file: `docs/foundation.md`
        """
    ).lstrip("\n")
    body = (
        deliverables
        + "\n## M1 Task Manifest\n`kind: manifest`\n\n```yaml\n"
        + manifest_yaml.rstrip()
        + "\n```\n"
    )
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_signature() -> None:
    """2.21a.1 — public surface: emit_stub_manifest signature and EmitOutcome literal."""
    sig = inspect.signature(emit_stub_manifest)
    plan_param = sig.parameters["plan_path"]
    assert plan_param.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    }
    by_actor = sig.parameters["by_actor"]
    assert by_actor.kind is inspect.Parameter.KEYWORD_ONLY
    assert by_actor.default == "dispatcher"
    plan_kind_param = sig.parameters["plan_kind"]
    assert plan_kind_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert plan_kind_param.default is PlanKind.implementation
    override_param = sig.parameters["plan_id"]
    assert override_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert override_param.default is None
    assert set(get_args(EmitOutcome)) == {
        "fresh",
        "replaced_malformed",
        "noop_existing_valid",
        "fallback_force_approve",
    }


def test_fresh_emission(tmp_path: Path) -> None:
    """2.21a.2 — plan with no manifest gets one synthesized from deliverables."""
    plan = _plan_two_deliverables(tmp_path)

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    text = plan.read_text(encoding="utf-8")
    assert "## M1 Task Manifest" in text
    assert "`kind: manifest`" in text

    document = parse_plan(plan, parse_mode="expansion")
    assert len(document.manifest_entries) == 2
    by_section = {entry.source_section: entry for entry in document.manifest_entries}
    assert set(by_section) == {"1.1", "1.2"}
    assert "covers:demo-plan:1.1:1.1.1" in by_section["1.1"].labels
    assert "covers:demo-plan:1.2:1.2.1" in by_section["1.2"].labels


def test_noop_on_valid_existing(tmp_path: Path) -> None:
    """2.21a.3 — valid manifest is left untouched and returns noop_existing_valid."""
    plan = _plan_with_manifest_yaml(tmp_path, _valid_manifest_yaml())
    before_text = plan.read_text(encoding="utf-8")

    outcome = emit_stub_manifest(plan)

    assert outcome == "noop_existing_valid"
    assert plan.read_text(encoding="utf-8") == before_text


def test_replace_malformed(tmp_path: Path) -> None:
    """2.21a.4 — malformed manifest gets replaced with a freshly-synthesized one."""
    malformed = textwrap.dedent(
        """
        - title: "Stale"
          category: code
        """
    ).strip()
    plan = _plan_with_manifest_yaml(tmp_path, malformed)

    outcome = emit_stub_manifest(plan)

    assert outcome == "replaced_malformed"
    document = parse_plan(plan, parse_mode="expansion")
    assert len(document.manifest_entries) == 2
    titles = {entry.title for entry in document.manifest_entries}
    assert "Stale" not in titles


def test_fallback_force_approve_no_raise(tmp_path: Path) -> None:
    """2.21a.5 — deliverable missing **Acceptance:** triggers absorbed-failure fallback."""
    plan = _write(
        tmp_path / "broken.md",
        """
        > **Plan ID:** demo-plan

        ## 1.1 Broken
        `kind: deliverable`

        Prose without an acceptance block.
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fallback_force_approve"
    text = plan.read_text(encoding="utf-8")
    assert "## Yolo Fallbacks" in text


def test_default_assignment_and_tdd_by_category(tmp_path: Path) -> None:
    """2.21a.6 — synthesized entries default agent, task_type, and tdd by category."""
    plan = _write(
        tmp_path / "plan.md",
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Code work [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `a.py`

        ### 1.2 Docs work [category: docs]
        `kind: deliverable`

        **Acceptance:**
        - 1.2.1 — file: `b.md`

        ### 1.3 Refactor work [category: refactor]
        `kind: deliverable`

        **Acceptance:**
        - 1.3.1 — file: `c.py`

        ### 1.4 Config work [category: config]
        `kind: deliverable`

        **Acceptance:**
        - 1.4.1 — file: `d.yaml`

        ### 1.5 Research work [category: research]
        `kind: deliverable`

        **Acceptance:**
        - 1.5.1 — file: `e.md`

        ### 1.6 Test work [category: test]
        `kind: deliverable`

        **Acceptance:**
        - 1.6.1 — file: `f.py`

        ### 1.7 Planning work [category: planning]
        `kind: deliverable`

        **Acceptance:**
        - 1.7.1 — behavior: planning complete
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    document = parse_plan(plan, parse_mode="expansion")
    by_section = {entry.source_section: entry for entry in document.manifest_entries}

    assert by_section["1.1"].tdd is True
    assert by_section["1.2"].tdd is False
    assert by_section["1.3"].tdd is False
    assert by_section["1.4"].tdd is True
    assert by_section["1.5"].tdd is False
    assert by_section["1.6"].tdd is False
    assert by_section["1.7"].tdd is False

    assert by_section["1.1"].assigned_agent == "backend-developer"
    assert by_section["1.2"].assigned_agent == "tech-writer"
    assert by_section["1.3"].assigned_agent == "backend-developer"
    assert by_section["1.4"].assigned_agent == "backend-developer"
    assert by_section["1.5"].assigned_agent == "researcher"
    assert by_section["1.6"].assigned_agent == "backend-developer"
    assert by_section["1.7"].assigned_agent == "planner"

    for entry in document.manifest_entries:
        assert entry.task_type == "feature"


def test_synthesized_test_entry_uses_frontend_agent_for_frontend_signals(
    tmp_path: Path,
) -> None:
    plan = _write(
        tmp_path / "frontend-test.md",
        """
        > **Plan ID:** frontend-test-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 React component harness [category: test]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 - file: `web/src/components/Button.test.tsx`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    document = parse_plan(plan, parse_mode="expansion")
    assert document.manifest_entries[0].assigned_agent == "frontend-developer"


def test_synthesized_entry_preserves_section_depends_on(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "depends.md",
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 A [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `a.py`

        ### 1.2 B [category: code] (depends: 1.1)
        `kind: deliverable`

        **Acceptance:**
        - 1.2.1 — file: `b.py`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    document = parse_plan(plan, parse_mode="expansion")
    by_section = {entry.source_section: entry for entry in document.manifest_entries}
    assert list(by_section["1.2"].depends_on) == ["1.1"]


def test_synthesized_phase_dep_expands_to_phase_deliverables(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "phase-depends.md",
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 A [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `a.py`

        ### 1.2 B [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.2.1 — file: `b.py`

        ## P2 Phase 2
        `kind: framing`

        ### 2.1 C [category: code] (depends: P1, 1.1)
        `kind: deliverable`

        **Acceptance:**
        - 2.1.1 — file: `c.py`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    document = parse_plan(plan, parse_mode="expansion")
    by_section = {entry.source_section: entry for entry in document.manifest_entries}
    assert list(by_section["2.1"].depends_on) == ["1.1", "1.2"]


@pytest.mark.parametrize("dependency", ["P9", "P3a"])
def test_synthesized_unknown_phase_dep_falls_back(
    tmp_path: Path,
    dependency: str,
) -> None:
    plan = _write(
        tmp_path / f"unknown-{dependency}.md",
        f"""
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 A [category: code] (depends: {dependency})
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `a.py`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fallback_force_approve"
    assert "## Yolo Fallbacks" in plan.read_text(encoding="utf-8")


def test_synthesized_self_phase_dep_falls_back(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "self-phase.md",
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 A [category: code] (depends: P1)
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `a.py`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fallback_force_approve"


def test_emit_and_reparse_round_trips_with_no_plan_id(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "no-plan-id.md",
        """
        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Work [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `src/work.py`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    resolved_missing_id = resolve_plan_id(None)
    assert resolved_missing_id == "unknown"
    assert resolve_plan_id("demo-plan") == "demo-plan"
    assert MISSING_PLAN_ID_SENTINEL == "unknown"
    document = parse_plan(plan, parse_mode="expansion")
    labels = [label for entry in document.manifest_entries for label in entry.labels]
    assert labels == [f"covers:{resolved_missing_id}:1.1:1.1.1"]

    plan_with_id = _write(
        tmp_path / "with-plan-id.md",
        """
        > **Plan ID:** demo-plan

        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Work [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `src/work.py`
        """,
    )

    assert emit_stub_manifest(plan_with_id) == "fresh"
    document_with_id = parse_plan(plan_with_id, parse_mode="expansion")
    labels_with_id = [
        label for entry in document_with_id.manifest_entries for label in entry.labels
    ]
    assert labels_with_id == ["covers:demo-plan:1.1:1.1.1"]


def test_emit_and_reparse_task_filename_plan_id(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "task-12761-foo.md",
        """
        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Work [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `src/work.py`
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fresh"
    document = parse_plan(plan, parse_mode="expansion")
    labels = [label for entry in document.manifest_entries for label in entry.labels]
    assert document.plan_id == "12761"
    assert labels == ["covers:12761:1.1:1.1.1"]


def test_emit_plan_id_override_round_trips(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "plan.md",
        """
        ## P1 Phase 1
        `kind: framing`

        ### 1.1 Work [category: code]
        `kind: deliverable`

        **Acceptance:**
        - 1.1.1 — file: `src/work.py`
        """,
    )

    outcome = emit_stub_manifest(plan, plan_id="12761")

    assert outcome == "fresh"
    document = parse_plan(plan, parse_mode="expansion", plan_id_override="12761")
    labels = [label for entry in document.manifest_entries for label in entry.labels]
    assert labels == ["covers:12761:1.1:1.1.1"]


def test_yolo_fallback_audit_is_parser_safe(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "fallback-parser-safe.md",
        """
        > **Plan ID:** demo-plan

        ## 1.1 Broken
        `kind: deliverable`

        Prose without an acceptance block.
        """,
    )

    outcome = emit_stub_manifest(plan)

    assert outcome == "fallback_force_approve"
    with pytest.raises(PlanParseError) as exc_info:
        parse_plan(plan, parse_mode="expansion")
    messages = [message for _, message in exc_info.value.errors]
    assert any("missing **Acceptance:** block" in message for message in messages)
    assert not any("non-canonical heading" in message for message in messages)


def test_yolo_fallback_then_reemit_recovers(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "fallback-recovers.md",
        """
        > **Plan ID:** demo-plan

        ## 1.1 Broken [category: code]
        `kind: deliverable`

        Prose without an acceptance block.
        """,
    )

    first_outcome = emit_stub_manifest(plan)
    fixed_text = plan.read_text(encoding="utf-8").replace(
        "Prose without an acceptance block.",
        "Recovered implementation.\n\n**Acceptance:**\n- 1.1.1 — file: `src/recovered.py`",
    )
    plan.write_text(fixed_text, encoding="utf-8")

    second_outcome = emit_stub_manifest(plan)

    assert first_outcome == "fallback_force_approve"
    assert second_outcome == "fresh"
    parse_plan(plan, parse_mode="expansion")
