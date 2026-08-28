"""Contract tests for the bundled build-rule authoring skill."""

import re
from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import RuleTriggerEvent

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "build-rule"
    / "SKILL.md"
)


def _skill_content() -> str:
    references = SKILL_PATH.parent / "references"
    return "\n\n".join(
        [
            SKILL_PATH.read_text(encoding="utf-8"),
            *(path.read_text(encoding="utf-8") for path in sorted(references.glob("*.md"))),
        ]
    )


@pytest.mark.skill_tdd
def test_build_rule_lists_every_current_trigger_event() -> None:
    content = _skill_content()

    for event in RuleTriggerEvent:
        assert f"`{event.value}`" in content


@pytest.mark.skill_tdd
def test_build_rule_identifies_normalized_turn_boundaries() -> None:
    content = _skill_content()

    assert "`turn_start`" in content
    assert "`turn_end`" in content
    assert "normalized turn boundaries" in content


@pytest.mark.skill_tdd
def test_build_rule_yaml_examples_parse() -> None:
    content = _skill_content()
    examples = re.findall(r"```yaml\n(.*?)```", content, flags=re.DOTALL)

    assert examples
    for example in examples:
        yaml.safe_load(example)
