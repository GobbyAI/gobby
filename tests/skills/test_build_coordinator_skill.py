"""Contract tests for the bundled build-coordinator skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/build-coordinator"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_build_coordinator_skill_parses_and_is_discoverable() -> None:
    parsed = parse_skill_file(SKILL_DIR / "SKILL.md")
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert parsed.name == "build-coordinator"
    assert "coordinator" in parsed.description.lower()
    assert "gobby build" in parsed.description.lower()
    assert "build-coordinator" in {skill.name for skill in skills}


def test_build_coordinator_separates_target_and_coordination_epic() -> None:
    body = _body()

    assert "separate coordination epic outside the target task tree" in body
    assert "Target task or epic: the user's product work" in body
    assert "Coordination epic: build coordination" in body
    assert "Do not close the target task or epic" in body
    assert "all discovered `gobby build` bugs from the run are closed" in body


def test_build_coordinator_documents_unattended_build_discipline() -> None:
    body = _body()

    assert "coordinator intervention as evidence" in body
    assert "current coordinator session" in body
    assert "Do not\ncreate or switch to a separate agent definition" in body
    assert "$gobby build-coordinator <target-ref>" in body
    assert "/gobby build-coordinator" in body
    assert "without `--quick`" in body
    assert "manual-ticking the dispatcher" in body
    assert "daemon-owned automation" in body
    assert "Use `wait_for_agent` only when no useful coordinator work is available" in body
    assert "compact context with `compact_self`" in body


def test_build_coordinator_documents_compact_self_tool_path() -> None:
    body = _body()

    assert "gobby-sessions:compact_self" in body
    assert 'list_tools(server_name="gobby-sessions")' in body
    assert 'get_tool_schema(server_name="gobby-sessions", tool_name="compact_self")' in body
    assert (
        'call_tool("gobby-sessions", "compact_self", {"session_id": "<current-session>"})' in body
    )
    assert "Use the Gobby session ref for `session_id`" in body


def test_build_coordinator_requires_stage_normalization_and_bug_fixes() -> None:
    body = _body()

    assert "Normalize leaf task stages" in body
    assert "default leaf tasks to `development`" in body
    assert "Fix blocking bugs immediately" in body
    assert "Fix non-blocking bugs when agents are running" in body
    assert "All discovered unattended-build bugs must be fixed" in body
    assert "committed, linked, and closed before the target" in body


def test_build_coordinator_is_generic_not_one_off() -> None:
    body = _body()

    assert "#12746" not in body
    assert "Neo4j" not in body
    assert "FalkorDB" not in body
    assert "<target-ref>" in body
