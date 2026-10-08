"""Contract tests for the coordination seat definitions.

The assistant, orchestrator and lane-manager seats are bundled definitions whose
prompt is the role text of record. They share the seat shape of
agent-definition-profiles P3 and differ only in their role contracts.
"""

from __future__ import annotations

from pathlib import Path
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

SEATS = ("assistant", "orchestrator", "lane-manager", "merge-manager", "inbox-manager")
# Decision 8: the write tools a read-only seat may not call.
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
# Orchestrator ruling on #22996: the seats route to sessions, and only the
# Orchestrator sends global cutover notices.
SEND_MESSAGE_TARGETS = {
    "assistant": {"parent", "session", "project"},
    "orchestrator": {"parent", "session", "project", "global"},
    "lane-manager": {"parent", "session", "project"},
    "merge-manager": {"parent", "session", "project"},
    "inbox-manager": {"parent", "session"},
}
# Worker-safety rules a spawned orchestrator drops to keep restart, cutover and
# worktree-cleanup authority.
ORCHESTRATOR_AUTHORITY_RULES = frozenset(
    {"no-daemon-management", "no-daemon-management-http", "block-git-worktree-mutations"}
)


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


@pytest.mark.parametrize("name", SEATS)
def test_seat_carries_the_common_seat_shape(name: str) -> None:
    raw = _raw(name)
    body = _load(name)
    prompt = _prompt(name)

    assert raw["tags"] == ["gobby", "seat"]
    assert set(raw) <= set(AgentDefinitionBody.model_fields) | SYNC_METADATA_KEYS
    assert body.name == name
    assert body.version == "1.0"
    assert body.surfaces == ["spawn", "persona"]
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
    assert {"tag:default", "tag:roles", "tag:worker-safety"} <= set(include)
    assert "tag:review-learning" not in include
    assert body.workflows.variables == {}
    assert body.step_workflow is None
    assert body.blocked_mcp_tools == ["gobby-agents:kill_agent"]
    assert set(body.send_message_targets) == SEND_MESSAGE_TARGETS[name]
    for section in _baseline_sections():
        assert section in prompt
    assert "gobby-agents:send_message" in prompt
    assert "compact" in prompt
    assert "never /clear" in prompt
    assert "lesson" not in prompt.lower()


def test_assistant_routes_and_edits_tasks_only_when_josh_asks() -> None:
    body = _load("assistant")
    prompt = _prompt("assistant")

    assert "You route." in prompt
    assert "verbatim" in prompt
    assert "half-hour status" in prompt
    assert "Decisions needed:" in prompt
    assert "before and after every restart" in prompt
    assert "at most one line confirming the send" in prompt
    assert "`gobby-tasks:update_task` only when Josh asks" in prompt
    assert "assistant-write-scope" in prompt
    assert "two consecutive five-minute load readings above 30" in prompt
    assert "two consecutive readings below 24" in prompt
    assert not READ_ONLY_BLOCKED_TOOLS & set(body.blocked_tools)


def test_orchestrator_coordinates_restarts_and_edits_tasks_as_needed() -> None:
    body = _load("orchestrator")
    prompt = _prompt("orchestrator")

    assert "Coordination only" in prompt
    assert "restarts and cutovers" in prompt
    assert "global notice before" in prompt
    assert "DAEMON BACK" in prompt
    assert "Archivist" in prompt
    assert "buttons through the Assistant" in prompt
    assert "Edit tasks as needed" in prompt
    assert "other seats' edit requests" in prompt
    assert body.workflows.rule_selectors is not None
    assert {"name:no-force-push-interactive", "name:no-destructive-git-interactive"} <= set(
        body.workflows.rule_selectors.include
    )
    assert set(body.workflows.rule_selectors.exclude) == {
        f"name:{rule}" for rule in ORCHESTRATOR_AUTHORITY_RULES
    }


def test_spawned_orchestrator_keeps_restart_authority_and_git_protection(
    temp_db: HubDatabase,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    rows = RuleDefinitionManager(temp_db).list_all()

    def selected(name: str) -> set[str]:
        body = _load(name)
        return {row.name for row in rows if rule_matches_agent(body, row)}

    orchestrator = selected("orchestrator")
    assert not orchestrator & ORCHESTRATOR_AUTHORITY_RULES
    assert {
        "no-destructive-git",
        "no-force-push",
        "no-destructive-git-interactive",
        "no-force-push-interactive",
    } <= orchestrator
    for name in ("assistant", "lane-manager"):
        assert ORCHESTRATOR_AUTHORITY_RULES <= selected(name), name


def test_lane_manager_is_read_only_with_event_lines_and_hold_resume() -> None:
    body = _load("lane-manager")
    prompt = _prompt("lane-manager")

    assert set(body.blocked_tools) == READ_ONLY_BLOCKED_TOOLS
    assert (
        "LANE= EVENT=STARTED|CANDIDATE|BOUNCE|CLOSED TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE="
    ) in prompt
    assert "Obey HOLD and RESUME from the Orchestrator at once and ACK each one" in prompt
    assert "two consecutive five-minute load readings above 30" in prompt
    assert "two consecutive readings below 24" in prompt
    assert "gobby-agents:wait_for_coordination" in prompt
    assert "Check lanes, work with orchestrator to address blockers, keep agents working" in (
        prompt
    )
    assert "edit requests to the Orchestrator" in prompt


def test_lane_manager_resumes_stalled_seats_by_pane_only_on_the_persona_surface() -> None:
    # Orchestrator ruling on #22996 I1: spawned agents are refused the operator
    # tools capture_output and send_keys, so a spawned lane-manager escalates.
    prompt = _prompt("lane-manager")

    assert "operator tools a spawned agent is refused" in prompt
    assert "applies only in a pane (the persona surface)" in prompt
    assert "As a spawned lane-manager, report the stalled seat to the Orchestrator instead" in (
        prompt
    )
    assert "`continue: check your queued Gobby messages and proceed\\n`" in prompt


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


def test_merge_manager_tracks_activation_and_preserves_foreign_work() -> None:
    body = _load("merge-manager")
    prompt = _prompt("merge-manager")

    assert body.blocked_tools == []
    assert body.network == "none"
    assert body.spawnable_agents == []
    assert "# Merge Manager" in prompt
    assert "the reviewer calls `land_commit` immediately" in prompt
    assert "Keep the landed-but-unactivated ledger" in prompt
    assert "Only activation is batched" in prompt
    assert "Never batch landings under reservations" in prompt
    assert "create landing tasks or merge candidates into `0.5.0`" in prompt
    assert "Do not remove dirty worktrees or branches" in prompt
    assert "perform it only when explicitly assigned" in prompt
    assert "This seat never restarts, cuts over or promotes live binaries" in prompt
    assert "Never push or merge into `main`" in prompt
    role = Path(__file__).resolve().parents[2] / ".gobby/roles/merge-manager.md"
    assert flat(role.read_text()) in prompt


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
    assert "close backlog above 5" in prompt
    assert "Never spawn agents, create or update tasks, edit files, restart the daemon" in prompt
    assert "~/.gobby/local_cli_token" in prompt
    assert "~/.gobby/bootstrap.yaml" in prompt
