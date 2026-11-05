"""Bundled review-evidence gates key on the review kind, never on the reporting seat."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

BUNDLED_RULES = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/rules"
RECORD_LESSON_TOOL = "gobby-review-learning:record_review_lesson"
COMPLETE_FINDING = {
    "check_key": "fixture-check",
    "lesson_type": "fixture-miss",
    "prevention": "Fixture prevention.",
    "path": "src/fixture.py",
    "finding_fingerprint": "fixture-fingerprint",
    "principle": "Fixture principle.",
}
COMPLETE_EVIDENCE = {"leaf_task_ref": "#1", "verified_fix": "fixture-commit"}


def _review_gate_conditions() -> list[str]:
    conditions: list[str] = []
    for path in sorted(BUNDLED_RULES.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text()) or {}
        for rule in (document.get("rules") or {}).values():
            for effect in rule.get("effects") or []:
                if effect.get("type") == "block" and RECORD_LESSON_TOOL in (
                    effect.get("mcp_tools") or []
                ):
                    conditions.append(rule["when"])
    return conditions


def _gate_fires(condition: str, tool_input: dict[str, Any]) -> bool:
    context = {"tool_input": tool_input}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    return evaluator.evaluate(condition)


def test_a_bundled_gate_covers_review_lessons() -> None:
    assert _review_gate_conditions()


@pytest.mark.parametrize("source", ["fixture-reviewer", "fixture-epic-reviewer", "fixture-seat"])
def test_incomplete_review_kind_is_refused_whichever_seat_reports_it(source: str) -> None:
    tool_input = {"source_kind": "qa_rejection", "source": source, "finding": {}, "evidence": {}}
    for condition in _review_gate_conditions():
        assert _gate_fires(condition, tool_input)


def test_complete_review_kind_is_allowed() -> None:
    tool_input = {
        "source_kind": "qa_rejection",
        "source": "fixture-reviewer",
        "finding": COMPLETE_FINDING,
        "evidence": COMPLETE_EVIDENCE,
    }
    for condition in _review_gate_conditions():
        assert not _gate_fires(condition, tool_input)


def test_other_review_kinds_are_not_gated() -> None:
    tool_input = {"source_kind": "plan_review", "source": "fixture-reviewer", "finding": {}}
    for condition in _review_gate_conditions():
        assert not _gate_fires(condition, tool_input)
