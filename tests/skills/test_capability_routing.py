"""Behavioral contract for capability and standalone routing."""

from pathlib import Path

import pytest

from gobby.cli.installers.skill_install import (
    install_router_skills_as_cli_skills,
    install_router_skills_as_commands,
)
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.capability_routing import route_gobby_request, standalone_menu
from gobby.skills.parser import ParsedSkill

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prefix", ["$gobby", "/gobby"])
def test_reference_contract_3_1_1(prefix: str) -> None:
    """Menus are neutral; explicit topic selection loads just that reference."""
    collision = ParsedSkill(name="tasks", description="Custom method", content="PRIVATE BODY")
    resolver = {"tasks": collision}.get
    help_route = route_gobby_request("help", resolve_skill=resolver, command_prefix=prefix)
    assert f"{prefix} tasks" in help_route.context
    assert "get_skill" not in help_route.context
    overview = route_gobby_request("tasks", resolve_skill=resolver)
    assert '"path":"references/tasks/overview.md"' in overview.context
    assert "PRIVATE BODY" not in overview.context
    menu = route_gobby_request("tasks references", resolve_skill=resolver, command_prefix=prefix)
    assert f"{prefix} tasks references closing" in menu.context
    assert "Commit and close" in menu.context
    assert "get_skill_file" not in menu.context
    topic = route_gobby_request("tasks references closing", resolve_skill=resolver)
    assert '"path":"references/tasks/closing.md"' in topic.context
    assert "overview.md" not in topic.context
    assert "get_tool_schema" in topic.context
    assert "cursor" in topic.context
    request = route_gobby_request("tasks close #42", resolve_skill=resolver)
    assert request.arguments == "close #42"
    assert "close #42" not in request.context
    assert "references/tasks/closing.md: Closing" in request.context
    explicit = route_gobby_request("skill tasks custom request", resolve_skill=resolver)
    assert explicit.kind == "skill"
    assert explicit.arguments == "custom request"
    assert '"name":"tasks"' in explicit.context
    unknown_topic = route_gobby_request("tasks references nonexistent", resolve_skill=resolver)
    assert unknown_topic.kind == "menu"
    assert "Unknown tasks reference" in unknown_topic.context
    assert "get_skill_file" not in unknown_topic.context


def test_skill_levels_arguments_and_internal_visibility() -> None:
    brevity = ParsedSkill(
        name="brevity",
        description="Concise responses",
        content="PRIVATE METHOD",
        metadata={"gobby": {"levels": ["lite", "normal", "max"], "default_level": "normal"}},
    )
    internal = ParsedSkill(name="internal", description="Hidden", content="", internal=True)
    collision = ParsedSkill(name="tasks", description="Custom tasks", content="")
    skills = [brevity, internal, collision]
    lookup = {skill.name: skill for skill in skills}.get
    route = route_gobby_request("brevity max\nkeep all facts", resolve_skill=lookup)
    assert '"level": "max"' in route.context
    assert route.arguments == "max\nkeep all facts"
    assert "keep all facts" not in route.context
    assert "PRIVATE METHOD" not in route.context
    default = route_gobby_request("brevity", resolve_skill=lookup)
    assert "level" not in default.context
    menu = standalone_menu(skills, load_capability_catalog(), "$gobby")
    assert "$gobby brevity" in menu
    assert "$gobby skill tasks" in menu
    assert "internal" not in menu
    assert route_gobby_request("skill internal", resolve_skill=lookup).kind == "skill"
    assert route_gobby_request("connected-server", resolve_skill=lookup).kind == "unknown"


def test_provider_carriers_use_catalog_without_reference_bodies(tmp_path: Path) -> None:
    command_dir = tmp_path / "commands"
    skill_dir = tmp_path / "skills"
    assert install_router_skills_as_commands(command_dir) == ["gobby.md"]
    assert install_router_skills_as_cli_skills(skill_dir) == ["gobby/"]
    command = (command_dir / "gobby.md").read_text()
    skill = (skill_dir / "gobby" / "SKILL.md").read_text()
    assert command == skill
    for capability in load_capability_catalog().capabilities:
        assert capability.description in command
    assert "get_skill_file" in command
    assert "## Exact Interactive Close Sequence" not in command
