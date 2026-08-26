"""Contract tests for the bundled memory skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_SKILL = REPO_ROOT / "src/gobby/install/shared/skills/memory/SKILL.md"


def test_memory_skill_is_search_first() -> None:
    content = MEMORY_SKILL.read_text(encoding="utf-8")

    assert 'version: "1.2.0"' in content
    assert "## Search" in content
    assert "- At task claim: search the task subject before editing" in content
    assert "- Before working in an unfamiliar subsystem." in content
    assert "- Before creating a memory" in content
    assert "`rationale`, `similarity`, and `memory_type`" in content
    for stale in ("## Recall", "injected recall", "automatically recalls", "injects at most"):
        assert stale not in content


def test_memory_skill_routes_plan_drafts_to_plan_artifacts() -> None:
    content = MEMORY_SKILL.read_text(encoding="utf-8")

    assert (
        "| Draft direction, implementation approach, enhancement suggestion, or review finding "
        "| Plan or evidence |"
    ) in content
    assert "Never store bugs as memories." in content
