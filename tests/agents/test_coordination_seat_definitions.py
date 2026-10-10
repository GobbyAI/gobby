"""Contract tests for the coordination seat definitions.

The assistant and orchestrator are root persona panes and are never spawned. The
lane-manager, release-manager and inbox-manager seats are spawned coordination
seats. Each definition is the role text of record for its seat.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.selectors import rule_matches_agent
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

SEATS = ("assistant", "orchestrator", "lane-manager", "release-manager", "inbox-manager")
# The root panes carry only the persona surface; every other seat also spawns.
SURFACES = {
    "assistant": ["persona"],
    "orchestrator": ["persona"],
    "lane-manager": ["spawn", "persona"],
    "release-manager": ["spawn", "persona"],
    "inbox-manager": ["spawn", "persona"],
}
SEND_MESSAGE_TARGETS = {
    "assistant": {"parent", "session", "agent", "project", "global", "build"},
    "orchestrator": {"parent", "session", "agent", "project", "global", "build"},
    "lane-manager": {"parent", "session", "project"},
    "release-manager": {"parent", "session", "project"},
    "inbox-manager": {"parent", "session"},
}
SPAWNABLE_AGENTS = {
    "assistant": [],
    "orchestrator": [],
    "lane-manager": [],
    "release-manager": ["task-close-reviewer"],
    "inbox-manager": [],
}
# The write tools a read-only seat may not call.
READ_ONLY_BLOCKED_TOOLS = frozenset(
    {
        "Edit",
        "KillShell",
        "MultiEdit",
        "NotebookEdit",
        "Write",
        "apply_patch",
        "edit_file",
        "notebook_edit",
        "replace",
        "write_file",
    }
)
# Sync metadata keys `validate_workflow_definition_data` strips before the body.
SYNC_METADATA_KEYS = frozenset({"tags", "priority", "type"})
# Terminal control (send_keys and capture_output) is opt-in: every other definition
# selects the no-send-keys rule by name.
KEEPS_TERMINAL_CONTROL = frozenset({"assistant", "orchestrator", "lane-manager"})
# Worker-safety rules that keep daemon restarts and worktree cleanup off spawned seats.
RESTART_AND_WORKTREE_RULES = frozenset(
    {"no-daemon-management", "no-daemon-management-http", "block-git-worktree-mutations"}
)
# Interactive git guards that the root panes select through tag:default.
ROOT_GIT_GUARDS = frozenset({"no-destructive-git-interactive", "no-force-push-interactive"})


def _raw(name: str) -> dict[str, Any]:
    data = yaml.safe_load((get_bundled_agents_path() / f"{name}.yaml").read_text())
    assert isinstance(data, dict), name
    return data


def _load(name: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(_raw(name))


def _prompt(name: str) -> str:
    prompts = _load(name).prompts
    assert prompts is not None, name
    return flat(prompts.agent)


def _section(text: str, heading: str) -> str:
    """``heading`` and its body, up to the next level-2 heading."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def _baseline_sections() -> list[str]:
    persona = _raw("default")["prompts"]["persona"]
    assert isinstance(persona, str)
    return [flat(_section(persona, heading)) for heading in ("## Platform Context", "## Skills")]


def _selected_rules(name: str, db: HubDatabase) -> set[str]:
    sync_bundled_rules(db, get_bundled_rules_path())
    body = _load(name)
    return {
        row.name for row in RuleDefinitionManager(db).list_all() if rule_matches_agent(body, row)
    }


@pytest.mark.parametrize("name", SEATS)
def test_seat_carries_the_common_seat_shape(name: str) -> None:
    raw = _raw(name)
    body = _load(name)
    prompt = _prompt(name)

    assert raw["tags"] == ["gobby", "seat"]
    assert set(raw) <= set(AgentDefinitionBody.model_fields) | SYNC_METADATA_KEYS
    assert body.name == name
    assert body.version == "1.0"
    assert body.surfaces == SURFACES[name]
    assert (body.provider, body.model, body.checkout_mode, body.timeout) == (
        "inherit",
        None,
        "inherit",
        0,
    )
    assert body.prompts is not None
    assert body.prompts.persona == body.prompts.agent
    assert body.workflows.rule_selectors is not None
    include = body.workflows.rule_selectors.include
    assert "tag:default" in include
    assert "tag:review-learning" not in include
    assert body.workflows.variables == {}
    assert body.step_workflow is None
    assert body.blocked_mcp_tools == ["gobby-agents:kill_agent"]
    assert set(body.send_message_targets) == SEND_MESSAGE_TARGETS[name]
    assert body.spawnable_agents == SPAWNABLE_AGENTS[name]
    for section in _baseline_sections():
        assert section in prompt
    assert "gobby-agents:send_message" in prompt
    assert "compact" in prompt
    assert "never /clear" in prompt
    assert "lesson" not in prompt.lower()


@pytest.mark.parametrize("name", ("assistant", "orchestrator"))
def test_root_panes_select_only_the_default_rules(name: str) -> None:
    selectors = _load(name).workflows.rule_selectors
    assert selectors is not None
    assert selectors.include == ["tag:default"]
    assert selectors.exclude == ["name:block-update-task"]


def test_assistant_routes_and_edits_tasks_only_when_the_operator_asks() -> None:
    body = _load("assistant")
    prompt = _prompt("assistant")

    assert "You turn what the operator says into work for the Orchestrator" in prompt
    assert "Relay the operator's instructions to the Orchestrator verbatim" in prompt
    assert "The Reporter drafts the 30-minute status" in prompt
    assert "Decisions needed:" in prompt
    assert "Restart the daemon or the terminal client only on the operator's go" in prompt
    assert "`gobby-tasks:update_task` only when the operator asks" in prompt
    assert "Reply once, on the channel the operator last used" in prompt
    assert "assistant-write-scope" in prompt
    assert "Recover stalled sessions" in prompt
    assert "Report each recovery to the Orchestrator" in prompt
    assert not READ_ONLY_BLOCKED_TOOLS & set(body.blocked_tools)


def test_orchestrator_coordinates_restarts_and_edits_tasks_as_needed() -> None:
    prompt = _prompt("orchestrator")

    assert "You coordinate the fleet" in prompt
    assert "Run restarts and cutovers with a global notice before the daemon stops" in prompt
    assert "a DAEMON BACK notice after it is healthy" in prompt
    assert "send the Archivist an update on every land" in prompt
    assert "with decisions as buttons" in prompt
    assert "Edit tasks as needed" in prompt
    assert "other seats' edit requests" in prompt
    assert "Recover stalled sessions" in prompt
    assert "Report each recovery to the Assistant" in prompt


def test_root_panes_keep_interactive_git_guards_without_worker_rules(temp_db: HubDatabase) -> None:
    for name in ("assistant", "orchestrator"):
        selected = _selected_rules(name, temp_db)
        assert ROOT_GIT_GUARDS <= selected, name
        assert not RESTART_AND_WORKTREE_RULES & selected, name


def test_spawned_coordination_seats_keep_restart_and_worktree_guards(temp_db: HubDatabase) -> None:
    for name in ("lane-manager", "release-manager"):
        assert RESTART_AND_WORKTREE_RULES <= _selected_rules(name, temp_db), name


def test_lane_manager_is_read_only_with_event_lines_and_hold_resume() -> None:
    body = _load("lane-manager")
    prompt = _prompt("lane-manager")

    assert set(body.blocked_tools) == READ_ONLY_BLOCKED_TOOLS
    assert (
        "LANE= EVENT=STARTED|CANDIDATE|BOUNCE|CLOSED TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE="
    ) in prompt
    assert "Obey HOLD and RESUME from the Orchestrator at once and ACK each one" in prompt
    assert "gobby-agents:wait_for_coordination" in prompt
    assert "Check lanes, work with orchestrator to address blockers, keep agents working" in (
        prompt
    )
    assert "edit requests to the Orchestrator" in prompt
    assert "sent unprompted to the Assistant" in prompt
    assert "the project's operating policy" in prompt


def test_lane_manager_recovers_stalled_seats_only_without_a_recovery_steward() -> None:
    prompt = _prompt("lane-manager")

    assert "The Recovery Steward owns patrol and recovery of stalled seats" in prompt
    assert "Only when no Recovery Steward is running" in prompt
    assert "`gobby-sessions:send_keys` with literal text plus a trailing newline" in prompt
    assert "`continue: check your queued Gobby messages and proceed\\n`" in prompt
    assert "hand that to the Assistant without reading it" in prompt
    assert "refused" not in prompt
    assert "As a spawned lane-manager" not in prompt


def test_release_manager_loads_as_the_landing_and_activation_ledger() -> None:
    prompt = _prompt("release-manager")

    for heading in ("Landing ledger.", "Close queue.", "Activation.", "Heavy-run slots."):
        assert heading in prompt, heading
    assert "`promote_workspace_binary_set`" in prompt
    assert "`record_close_receipt`" in prompt
    assert "Restarts and cutovers belong to the Orchestrator" in prompt


@pytest.mark.parametrize(
    "name",
    (
        "assistant",
        "orchestrator",
        "lane-manager",
        "release-manager",
        "inbox-manager",
        "researcher",
        "archivist",
    ),
)
def test_terminal_control_is_opt_in_per_definition(name: str, temp_db: HubDatabase) -> None:
    selected = _selected_rules(name, temp_db)
    assert ("no-send-keys" in selected) is (name not in KEEPS_TERMINAL_CONTROL), name


def test_coordination_seats_sync_as_installed_rows(definition_db: PostgresHubDatabase) -> None:
    result = sync_bundled_agents(definition_db)

    assert result["success"] is True
    assert result["errors"] == []
    manager = AgentDefinitionManager(definition_db)
    for name in SEATS:
        row = manager.get_by_name(name)
        assert row is not None, name
        assert row.enabled is True, name
        assert AgentDefinitionBody.model_validate(row.definition_json) == _load(name), name


def test_inbox_manager_is_read_only_and_routes_urgent_messages() -> None:
    body = _load("inbox-manager")
    prompt = _prompt("inbox-manager")

    assert set(body.blocked_tools) == READ_ONLY_BLOCKED_TOOLS
    assert body.network == "none"
    assert body.spawnable_agents == []
    assert body.prewarm_pre_commit_store is False
    assert "# Inbox Manager" in prompt
    assert "Work read-only in the main checkout" in prompt
    assert "Every five minutes, send the Orchestrator ONE digest with wake=false" in prompt
    assert "Forward at once" in prompt
    assert "with wake=true" in prompt
    assert 'single word "steady"' in prompt
    assert "close backlog above 10" in prompt
    assert "close backlog above 5" not in prompt
    assert "Never spawn agents, create or update tasks, edit files, restart the daemon" in prompt
    assert "local_cli_token" not in prompt
    assert "~/.gobby/bootstrap.yaml" in prompt
    assert "Keep one five-minute recurring reminder" in prompt
    assert "Lane, order, landing and close decisions belong to the Orchestrator" in prompt
