"""Behavior and source contracts for the bundled brevity skill."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = Path(__file__).resolve().parent / "scenarios/brevity/concise-readable-output.yaml"
SKILL_PATH = ROOT / "src/gobby/install/shared/skills/brevity/SKILL.md"
DRIFT_RULE_PATH = (
    ROOT / "src/gobby/install/shared/workflows/rules/brevity/detect-brevity-drift.yaml"
)
CURRENT_SKILL_FOOTPRINT = 3_564
CJK_PATTERN = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
ENGLISH_BANNED_LITERALS = (
    "Great question",
    "I'd be happy to",
    "Certainly",
    "Of course",
    "Let me break this down",
    "It's worth noting",
    "In summary",
    "In conclusion",
    "Hope this helps",
    "Feel free to ask",
    "If you want, I can also",
    "If you tell me",
    "My next step could be",
    "in other words",
)


def _extract_match_literals(expression: str) -> tuple[str, ...]:
    prefix = "assistant_response_matches_any(["
    payload = expression.split(prefix, maxsplit=1)[1].split("])", maxsplit=1)[0]
    parsed = ast.literal_eval(f"[{payload}]")
    assert isinstance(parsed, list)
    return tuple(str(item) for item in parsed)


def _load_drift_rules() -> dict[str, Any]:
    data = yaml.safe_load(DRIFT_RULE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    rules = data["rules"]
    assert isinstance(rules, dict)
    return rules


def test_brevity_pressure_scenario_preserves_meaning_and_readability() -> None:
    result = run_recorded_skill_scenario(SCENARIO)

    assert result.baseline.action_names == ("compress_output",)
    assert result.loaded.action_names == (
        "preserve_exact_content",
        "preserve_semantic_negation",
        "compress_output",
    )
    assert result.has_behavioral_delta


def test_brevity_skill_is_english_only_and_within_existing_footprint() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")

    assert CJK_PATTERN.search(content) is None
    assert len(content.encode("utf-8")) <= CURRENT_SKILL_FOOTPRINT


def test_brevity_skill_keeps_interface_and_adds_clarity_contract() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")

    assert "name: brevity" in content
    assert 'version: "1.2.0"' in content
    assert 'description: "Use when responses should be concise or use fewer tokens."' in content
    for trigger in (
        "brevity",
        "terse mode",
        "less tokens",
        "be brief",
        "stop slop",
        "stop brevity",
    ):
        assert f"  - {trigger}" in content
    assert "levels: [lite, normal, max]" in content
    assert "default_level: normal" in content
    assert "### Lite" in content
    assert "### Normal (default)" in content
    assert "### Max" in content
    assert 'get_skill(name="brevity", level="max")' in content
    assert 'When the user says "stop brevity"' in content

    expected_guidance = (
        "Keep meaning-critical `not`, `never`, `no`, `only`, and `except`.",
        "Permit standard technical acronyms. Never invent abbreviations or use arrow shorthand.",
        "Prefer clear grammar when alternatives cost the same number of tokens.",
        "Use short active sentences, imperatives, stable terms, and one statement per fact.",
        "Drop articles only in natural, unambiguous fragments.",
        "Apply brevity to conversational prose. Preserve the requested tone of task artifacts.",
    )
    for guidance in expected_guidance:
        assert guidance in content


def test_brevity_drift_source_is_english_only_with_unchanged_detection_contract() -> None:
    content = DRIFT_RULE_PATH.read_text(encoding="utf-8")
    rules = _load_drift_rules()

    assert CJK_PATTERN.search(content) is None
    assert tuple(rules) == (
        "detect-brevity-literal-drift",
        "detect-brevity-contrastive-drift",
    )

    literal_rule = rules["detect-brevity-literal-drift"]
    assert literal_rule["event"] == "turn_end"
    assert literal_rule["priority"] == 30
    assert _extract_match_literals(literal_rule["when"]) == ENGLISH_BANNED_LITERALS
    assert _extract_match_literals(literal_rule["effects"][0]["value"]) == ENGLISH_BANNED_LITERALS

    contrastive_rule = rules["detect-brevity-contrastive-drift"]
    assert contrastive_rule["event"] == "turn_end"
    assert contrastive_rule["priority"] == 31
