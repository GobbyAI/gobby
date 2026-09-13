"""Contract tests for build entrypoint routing and launch guidance."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REFERENCES = Path("src/gobby/install/shared/skills/gobby/references/build")


def test_build_skill_exists_and_delegates_to_shared_build_surface() -> None:
    content = " ".join((REFERENCES / "starting.md").read_text().split())
    for expected in (
        "gobby-tasks-ops:build_task",
        "loading its schema",
        "task reference",
        "Markdown plan path",
        "requested overrides",
        "existing user authorization suffices",
        "`isolation` is `none`, `worktree`, or `clone`",
        "`no_merge`",
        "`stage`",
        "`max_retries=0`",
        "`max_active_agents`",
        "`quick=true`",
        "successful request is not a completed build",
    ):
        assert expected in content
    assert "--yolo" not in content


def test_build_skill_points_to_coordinator_for_e2e_and_debugging() -> None:
    overview = (REFERENCES / "overview.md").read_text()
    assert "coordination" in overview
    assert "monitoring" in overview
    content = (REFERENCES / "coordination.md").read_text()
    assert "validating/debugging unattended automation" in content
    assert "coordinator role" in content
    assert "separate coordinator agent" in content
