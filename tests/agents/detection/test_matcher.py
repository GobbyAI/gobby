"""Fixture-driven tests for compiled detection manifests."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents.detection.matcher import compile_manifest

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_priority_and_regions() -> None:
    content = (FIXTURES / "detection_manifest.toml").read_text(encoding="utf-8")
    pane = (FIXTURES / "pane_approval.txt").read_text(encoding="utf-8")
    compiled = compile_manifest(content)

    prompt_box = compiled.match(pane)
    assert prompt_box.match is not None
    assert prompt_box.match.rule_id == "trust_prompt"
    assert prompt_box.match.state == "blocked"

    without_box = compiled.match(pane.split("╭", maxsplit=1)[0])
    assert without_box.match is not None
    assert without_box.match.rule_id == "approval_prompt"

    excluded = compiled.match(
        "historical approval marker\nDo you want to proceed?\n❯ 1. Yes\nesc to interrupt\n"
    )
    assert excluded.match is not None
    assert excluded.match.rule_id == "historical_stall"


def test_compile_cache_uses_content_fingerprint() -> None:
    content = (FIXTURES / "detection_manifest.toml").read_text(encoding="utf-8")

    first = compile_manifest(content)
    cached = compile_manifest(content)
    edited = compile_manifest(content.replace("priority = 900", "priority = 901"))

    assert cached is first
    assert edited is not first
    assert edited.fingerprint != first.fingerprint
    assert edited.manifest.version == first.manifest.version


def test_invalid_pattern_flags_rule_and_falls_through() -> None:
    content = """
id = "claude"
version = "1"
engine = 1

[[rules]]
id = "broken"
state = "working"
priority = 100
region = "whole_recent"
line_regex = ["("]

[[rules]]
id = "fallback"
state = "idle"
priority = 1
region = "whole_recent"
contains = ["ready"]
"""

    compiled = compile_manifest(content)
    evaluation = compiled.match("ready")

    assert [issue.code for issue in compiled.issues] == ["invalid_pattern"]
    assert evaluation.match is not None
    assert evaluation.match.rule_id == "fallback"


def test_pattern_timeout_flags_rule_and_falls_through() -> None:
    content = """
id = "claude"
version = "1"
engine = 1

[[rules]]
id = "pathological"
state = "working"
priority = 100
region = "whole_recent"
line_regex = ["(a+)+$"]

[[rules]]
id = "fallback"
state = "idle"
priority = 1
region = "whole_recent"
contains = ["ready"]
"""

    evaluation = compile_manifest(content).match("a" * 100_000 + "! ready")

    assert evaluation.match is not None
    assert evaluation.match.rule_id == "fallback"
    assert [issue.code for issue in evaluation.issues] == ["pattern_timeout"]
