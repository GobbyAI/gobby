"""Recorded skill-TDD scenario runner.

The runner keeps skill behavior tests deterministic: each scenario records the
baseline behavior without a skill and the expected behavior with that skill
loaded. Live agent spawning can replace the recorded source later without
changing the scenario contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ScenarioRun:
    name: str
    loaded_skills: tuple[str, ...]
    actions: tuple[dict[str, Any], ...]

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(str(action["action"]) for action in self.actions)

    @property
    def combined_text(self) -> str:
        return "\n".join(str(action.get("text", "")) for action in self.actions)


@dataclass(frozen=True)
class SkillScenarioResult:
    skill: str
    baseline: ScenarioRun
    loaded: ScenarioRun

    @property
    def has_behavioral_delta(self) -> bool:
        return self.baseline.actions != self.loaded.actions


def run_recorded_skill_scenario(path: Path) -> SkillScenarioResult:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    skill = str(data["skill"])
    runs = data["runs"]
    assert isinstance(runs, dict)

    baseline = _load_run("excluded", runs["excluded"])
    loaded = _load_run("loaded", runs["loaded"])

    result = SkillScenarioResult(skill=skill, baseline=baseline, loaded=loaded)
    _assert_contract(result, data.get("assertions", {}))
    return result


def _load_run(name: str, data: dict[str, Any]) -> ScenarioRun:
    loaded_skills = tuple(str(skill) for skill in data.get("loaded_skills", ()))
    actions = tuple(dict(action) for action in data["actions"])
    return ScenarioRun(name=name, loaded_skills=loaded_skills, actions=actions)


def _assert_contract(result: SkillScenarioResult, assertions: dict[str, Any]) -> None:
    assert result.skill not in result.baseline.loaded_skills, (
        f"{result.skill}: baseline unexpectedly loaded {result.baseline.loaded_skills}"
    )
    assert result.skill in result.loaded.loaded_skills, (
        f"{result.skill}: loaded skills {result.loaded.loaded_skills} missing expected skill"
    )
    assert result.has_behavioral_delta, (
        f"{result.skill}: expected behavioral delta between excluded and loaded runs"
    )

    loaded_order = tuple(assertions.get("loaded_action_order", ()))
    if loaded_order:
        assert result.loaded.action_names == loaded_order, (
            f"{result.skill}: loaded action order {result.loaded.action_names} "
            f"!= expected {loaded_order}"
        )

    baseline_order = tuple(assertions.get("baseline_action_order", ()))
    if baseline_order:
        assert result.baseline.action_names == baseline_order, (
            f"{result.skill}: baseline action order {result.baseline.action_names} "
            f"!= expected {baseline_order}"
        )

    for text in assertions.get("baseline_text_contains", ()):
        assert str(text) in result.baseline.combined_text, (
            f"{result.skill}: baseline text missing expected fragment {text!r}"
        )

    for text in assertions.get("loaded_text_contains", ()):
        assert str(text) in result.loaded.combined_text, (
            f"{result.skill}: loaded text missing expected fragment {text!r}"
        )

    for text in assertions.get("loaded_text_forbids", ()):
        assert str(text) not in result.loaded.combined_text, (
            f"{result.skill}: loaded text included forbidden fragment {text!r}"
        )
