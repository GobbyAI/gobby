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


def test_build_skill_documents_interactive_e2e_validation_pattern() -> None:
    skill_path = Path("src/gobby/install/shared/skills/build/SKILL.md")

    content = skill_path.read_text()

    assert "coordinator/tracking epic" in content
    assert "automation target" in content
    assert "without `--quick`" in content
    assert "real merge SHA" in content
    assert "no agents are running" in content
    assert "no tasks remain claimed" in content
    assert "no stale build worktrees or clones" in content
    assert "root `README.md`" in content
    assert "shared build service is the source of truth" in content.lower()


def test_build_skill_forbids_changing_requirements_to_pass_e2e() -> None:
    skill_path = Path("src/gobby/install/shared/skills/build/SKILL.md")

    content = skill_path.read_text()

    assert "Do not make the test pass by changing the required agent" in content
    assert "provider" in content
    assert "lifecycle route" in content
    assert "task scope" in content
    assert "acceptance criteria" in content
    assert "preserving the requested path" in content
    assert "extreme edge case" in content
    assert "exhausting practical fixes" in content


def test_build_skill_forbids_manual_dispatcher_ticks_during_unattended_e2e() -> None:
    skill_path = Path("src/gobby/install/shared/skills/build/SKILL.md")

    content = skill_path.read_text()

    assert "launch `gobby build #epic ...` once" in content
    assert "daemon-owned automation" in content
    assert "manual dispatcher ticks" in content
    assert "anti-pattern" in content
    assert "can hide a broken dispatcher loop" in content
    assert "bounded explicit tick" in content
    assert "only as a diagnostic or recovery step" in content


def test_build_skill_documents_provider_neutral_automation_diagnostics() -> None:
    skill_path = Path("src/gobby/install/shared/skills/build/SKILL.md")

    content = skill_path.read_text()

    assert "Automation Debugging Pattern" in content
    assert "Compare against the last known successful run" in content
    assert "SessionStart activation completed" in content
    assert "first provider-neutral prompt event" in content
    assert "ensure_session_activation(session_id)" in content
    assert "Do not replay the raw SessionStart hook wholesale" in content
    assert "OpenTelemetry" in content
    assert "agent_run_id" in content
    assert "session_id" in content
