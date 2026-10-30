"""Behavioral contract for capability and standalone routing."""

from pathlib import Path

import pytest

from gobby.cli.installers.skill_install import (
    install_router_skills_as_cli_skills,
    install_router_skills_as_commands,
)
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.capability_routing import GobbyRoute, route_gobby_request, standalone_menu
from gobby.skills.parser import ParsedSkill
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_resolver import AgentResolutionError, resolve_cold_start_command
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.unit

_NO_SKILLS: dict[str, ParsedSkill] = {}


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
        assert capability.description not in command
    assert "get_skill_file" in command
    assert "## Exact Interactive Close Sequence" not in command


@pytest.mark.parametrize("prefix", ["$gobby", "/gobby"])
@pytest.mark.parametrize(
    ("command", "role"),
    [("admin", "orchestrator"), ("Assistant", "assistant"), ("Guide", "custom")],
)
def test_bare_operator_command_cold_starts_its_pane(prefix: str, command: str, role: str) -> None:
    """A bare admin or assistant names one deterministic tool call for that pane's role."""
    route = route_gobby_request(
        command,
        resolve_skill=_NO_SKILLS.get,
        command_prefix=prefix,
        resolve_cold_start=lambda name: {command.casefold(): role}.get(name.casefold()),
    )
    assert route.kind == "cold_start"
    assert route.arguments == ""
    assert 'get_tool_schema(server_name="gobby-agents", tool_name="cold_start_admin")' in (
        route.context
    )
    assert f'call_tool("gobby-agents", "cold_start_admin", {{"role": "{role}"}})' in route.context
    assert "report the seat refs" in route.context
    assert "references/admin/overview.md" not in route.context


def test_no_builtin_cold_start_without_definition_resolution() -> None:
    assert route_gobby_request("assistant", resolve_skill=_NO_SKILLS.get).kind == "unknown"
    assert route_gobby_request("admin", resolve_skill=_NO_SKILLS.get).kind == "reference"


def test_cold_start_alias_uses_enabled_definitions_and_rejects_ambiguity(
    temp_db: HubDatabase,
) -> None:
    seed_seat(
        temp_db,
        "custom-guide",
        surfaces=["persona"],
        prompts={"persona": "Guide this project."},
        cold_start_commands=["Guide"],
    )
    assert resolve_cold_start_command(temp_db, "GUIDE") == "custom-guide"
    definitions = AgentDefinitionManager(temp_db)
    row = definitions.get_by_name("custom-guide")
    assert row is not None
    definitions.update(row.id, enabled=False)
    assert resolve_cold_start_command(temp_db, "guide") is None
    definitions.update(row.id, enabled=True)
    seed_seat(
        temp_db,
        "other-guide",
        surfaces=["persona"],
        prompts={"persona": "Another guide."},
        cold_start_commands=["guide"],
    )
    with pytest.raises(AgentResolutionError, match="multiple definitions"):
        resolve_cold_start_command(temp_db, "guide")


def test_disabled_project_override_hides_global_cold_start_alias(
    temp_db: HubDatabase, sample_project: dict[str, object]
) -> None:
    seed_seat(
        temp_db,
        "guide",
        surfaces=["persona"],
        prompts={"persona": "Guide this project."},
        cold_start_commands=["guide"],
    )
    definitions = AgentDefinitionManager(temp_db)
    global_row = definitions.get_by_name("guide")
    assert global_row is not None
    project_id = str(sample_project["id"])
    definitions.create("guide", global_row.definition_json, project_id=project_id, enabled=False)
    assert resolve_cold_start_command(temp_db, "guide") == "guide"
    assert resolve_cold_start_command(temp_db, "guide", project_id=project_id) is None


def test_admin_requests_and_capability_loads_keep_the_admin_capability() -> None:
    """Arguments, references and non-operator carriers never cold start."""
    request = route_gobby_request("admin check daemon health", resolve_skill=_NO_SKILLS.get)
    assert request.kind == "reference"
    assert request.arguments == "check daemon health"
    assert '"path":"references/admin/overview.md"' in request.context
    assert route_gobby_request("admin references", resolve_skill=_NO_SKILLS.get).kind == "menu"
    loaded = route_gobby_request("admin", resolve_skill=_NO_SKILLS.get, operator_commands=False)
    assert loaded.kind == "reference"
    assert '"path":"references/admin/overview.md"' in loaded.context
    assert "cold_start_admin" not in loaded.context
    unknown = route_gobby_request(
        "assistant", resolve_skill=_NO_SKILLS.get, operator_commands=False
    )
    assert unknown == GobbyRoute("unknown", "", "", "assistant")
    custom = ParsedSkill(name="assistant", description="Custom", content="PRIVATE")
    explicit = route_gobby_request("skill assistant", resolve_skill={"assistant": custom}.get)
    assert explicit.kind == "skill"
    assert "cold_start_admin" not in explicit.context
