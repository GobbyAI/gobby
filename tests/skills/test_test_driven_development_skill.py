"""Contract tests for the bundled test-driven-development skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = (
    REPO_ROOT
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "test-driven-development"
    / "SKILL.md"
)
SKILL_DIR = SKILL_PATH.parent
SCENARIO_PATH = (
    REPO_ROOT
    / "tests"
    / "skills"
    / "scenarios"
    / "test-driven-development"
    / "rust-stub-first-red.yaml"
)


def test_skill_parses_with_rust_stub_first_guidance() -> None:
    parsed = SkillLoader().load_skill(SKILL_DIR)

    assert parsed.name == "test-driven-development"
    assert "## Rust Stub-First RED" in parsed.content


def test_rust_guidance_requires_stub_first_direct_red() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")

    rust = content.split("## Rust Stub-First RED", maxsplit=1)[1].split(
        "## Completion Evidence", maxsplit=1
    )[0]
    for expected in (
        "minimal API shape",
        "default",
        "stub",
        "`todo!()`",
        "cargo nextest run -p gobby-client -E 'test(agent_status_updates_sidebar)'",
        "assertion",
        "panic",
        "Compiler failures",
        "missing product types",
        "reconstructed RED",
    ):
        assert expected in rust

    assert rust.index("minimal API shape") < rust.index("cargo nextest run")
    assert rust.index("cargo nextest run") < rust.index("Implement the behavior")
    assert "do not count" in rust
    assert "after implementation" in rust


def test_rust_stub_first_pressure_scenario_has_behavioral_delta() -> None:
    result = run_recorded_skill_scenario(SCENARIO_PATH)

    assert result.loaded.action_names == (
        "write_rust_test",
        "add_minimal_api_stub",
        "run_direct_red",
        "implement_behavior",
        "run_direct_green",
        "respond",
    )
    assert "count_compile_failure_as_red" not in result.loaded.action_names
    assert "reconstruct_red_after_implementation" not in result.loaded.action_names
    assert result.has_behavioral_delta
