"""Red tests for the interactive /gobby build skill contract."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_build_skill_exists_and_delegates_to_shared_build_surface() -> None:
    skill_path = Path("src/gobby/install/shared/skills/build/SKILL.md")

    content = skill_path.read_text()

    assert "/gobby build" in content
    assert "gobby build" in content
    assert "shared build service" in content.lower()
    assert "plan file" in content.lower()
    assert "task ref" in content.lower()
    assert "/gobby plan" in content
    assert "quick" in content
    assert "--skip-stage" in content
    assert "--stage" in content
    assert "--clone" in content
    assert "--max-active-agents" in content
    assert "--isolation" not in content
    assert "--no-merge" in content
    assert "--yolo" not in content


def test_build_skill_points_to_coordinator_for_e2e_and_debugging() -> None:
    skill_path = Path("src/gobby/install/shared/skills/build/SKILL.md")

    content = skill_path.read_text()

    assert "build-coordinator" in content
    assert "Interactive E2E Validation" not in content
    assert "Automation Debugging Pattern" not in content
