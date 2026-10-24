"""Contract tests for the coordination seat definitions.

The assistant and orchestrator are root persona panes and are never spawned. The
dev-manager, release-manager and triage seats are spawned coordination seats.
Each definition is the role text of record for its seat.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.condition_helpers_sessions import send_message_target_allowed
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.selectors import rule_matches_agent
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

SEATS = ("assistant", "orchestrator", "dev-manager", "release-manager", "triage")
# The root panes carry only the persona surface; every other seat also spawns.
SURFACES = {
    "assistant": ["persona"],
    "orchestrator": ["persona"],
    "dev-manager": ["spawn", "persona"],
    "release-manager": ["spawn", "persona"],
    "triage": ["spawn", "persona"],
}
SEND_MESSAGE_TARGETS = {
    "assistant": {"parent", "session", "agent", "project", "global"},
    "orchestrator": {"parent", "session", "agent", "project", "global"},
    "dev-manager": {"parent", "session", "project"},
    # The release batch announces each activation globally.
    "release-manager": {"parent", "session", "project", "global"},
    "triage": {"parent", "session"},
}
# Seats with a procedure reference; the pane-placing seats also carry gclient's.
PROCEDURE_SEATS = ("orchestrator", "dev-manager", "triage")
PANE_SEATS = ("assistant", "orchestrator", "dev-manager")
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
KEEPS_TERMINAL_CONTROL = frozenset({"assistant", "orchestrator", "dev-manager"})
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
    body = _load(name)
    prompts = body.prompts
    assert prompts is not None, name
    prompt = prompts.agent or ""
    reference = body.workflows.variables.get("_seat_procedure")
    if reference:
        assert reference in prompt
        skill_root = get_bundled_agents_path().parents[1] / "skills/gobby"
        procedure = (skill_root / reference.split(":", 1)[1]).read_text()
        prompt += "\n" + procedure
        # Follow the further references the procedure tells the seat to load.
        for path in re.findall(r'get_skill_file\(name="gobby", path="([^"]+)"\)', procedure):
            prompt += "\n" + (skill_root / path).read_text()
    return flat(prompt)


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
    assert (body.provider, body.checkout_mode, body.timeout) == (
        "inherit",
        "inherit",
        0,
    )
    # Launch defaults choose every seat's model; Triage keeps its own reasoning effort.
    assert body.model is None
    assert body.reasoning_effort == ("medium" if name == "triage" else None)
    assert body.prompts is not None
    assert body.prompts.persona == body.prompts.agent
    assert body.workflows.rule_selectors is not None
    include = body.workflows.rule_selectors.include
    assert "tag:default" in include
    assert "tag:review-learning" not in include
    procedure = {"_seat_procedure": f"gobby:references/agents/procedures/{name}.md"}
    references = {"_seat_references": ["gobby:references/agents/gclient.md"]}
    assert body.workflows.variables == {
        **(procedure if name in PROCEDURE_SEATS else {}),
        **(references if name in PANE_SEATS else {}),
        # The seat-spawn policy's manager exemption reads this variable.
        **({"team_manager": True} if name == "dev-manager" else {}),
    }
    assert body.step_workflow is None
    assert body.blocked_mcp_tools == ["gobby-agents:kill_agent"]
    assert set(body.send_message_targets) == SEND_MESSAGE_TARGETS[name]
    assert body.spawnable_agents == []
    for section in _baseline_sections():
        assert section in prompt
    assert "gobby-agents:send_message" in prompt
    assert "compact" in prompt
    assert "never /clear" in prompt
    # Only the Dev Manager records lessons, in its closing step record.
    assert ("lesson" in prompt.lower()) is (name == "dev-manager")


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
    assert "The Reporter owns status at the project-policy cadence" in prompt
    assert "Only on a failed direct delivery" in prompt
    assert "send_operator_message" in prompt
    assert "Decisions needed:" in prompt
    assert "Restart the daemon or the terminal client only on the operator's go" in prompt
    assert "`gobby-tasks:update_task` only when the operator asks" in prompt
    assert "While attached to a configured conversation, type replies normally." in prompt
    assert "The daemon mirrors completed assistant text automatically." in prompt
    assert "Do not explicitly send the same reply to the channel or narrate a send." in prompt
    assert (
        "When unattached, reply once on the channel the operator last used, "
        "and keep replying there until input arrives on another channel."
    ) in prompt
    assert "assistant-write-scope" in prompt
    assert "Recover stalled sessions" in prompt
    assert "Report each recovery to the Orchestrator" in prompt
    assert not READ_ONLY_BLOCKED_TOOLS & set(body.blocked_tools)


def test_orchestrator_coordinates_restarts_and_edits_tasks_as_needed() -> None:
    prompt = _prompt("orchestrator")

    assert "You coordinate the fleet" in prompt
    assert "Run restarts and cutovers with a global notice before the daemon stops" in prompt
    assert "a DAEMON BACK notice after it is healthy" in prompt
    assert "get_handoff is allowed and required during the hold" in prompt
    assert "send the Archivist an update on every land" in prompt
    assert "with decisions as buttons" in prompt
    assert "Edit tasks as needed" in prompt
    assert "other seats' edit requests" in prompt
    assert "Recover stalled sessions" in prompt
    assert "Report each recovery to the Assistant" in prompt


def test_orchestrator_writes_every_step_record_only_as_a_last_resort() -> None:
    assert _load("orchestrator").task_step_writes == [
        "test",
        "implementation",
        "review",
        "closing",
    ]
    prompt = _prompt("orchestrator")
    assert "For a missing step record, delegate first" in prompt
    assert "an independent reviewer or a Dev Manager from outside the team" in prompt
    assert "only when every other avenue is exhausted" in prompt
    for name in ("assistant", "default"):
        assert _load(name).task_step_writes == [], name


def test_orchestrator_leaves_team_landings_to_the_dev_manager() -> None:
    prompt = _prompt("orchestrator")

    assert "landing_approval" not in prompt
    assert "Its Dev Manager lands the approved SHA and closes the task" in prompt
    assert "`report_to=<Triage ref>`" in prompt
    assert "`report_to=<Dev Manager ref>`" in prompt
    assert "never substitute another reviewer identity" in prompt
    assert "Land with `gobby-tasks-ops:land_commit` only as the last resort" in prompt
    assert "An escalated refusal has already moved the task to Triage" in prompt
    for retired in ("EVENT=LANDED", "EVENT=ALREADY_LANDED", "EVENT=LAND_PENDING", "lane"):
        assert retired not in prompt, retired


def test_synced_seat_guidance_requires_handoff_during_restart_hold(temp_db: HubDatabase) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    row = RuleDefinitionManager(temp_db).get_by_name("inject-seat-common")
    assert row is not None
    prompt = flat(row.definition_json["effects"][0]["template"])
    assert "restart or cutover hold" in prompt
    assert "`gobby-sessions:get_handoff` is always allowed and required" in prompt
    assert "Do not wait for DAEMON BACK to consume it" in prompt


def test_consolidator_is_propose_only_with_read_only_task_tools() -> None:
    body = _load("consolidator")
    prompt = _prompt("consolidator")
    assert body.surfaces == ["spawn", "persona"]
    assert body.execution_mode == "interactive"
    assert body.timeout == 0
    assert set(body.blocked_tools) == READ_ONLY_BLOCKED_TOOLS
    assert body.step_workflow is not None
    assert [step.name for step in body.step_workflow.steps] == ["load_skills", "consolidate"]
    step = next(step for step in body.step_workflow.steps if step.name == "consolidate")
    assert step.allowed_tools != "all"
    assert not {"Bash", "exec_command", "Edit", "Write", "apply_patch"} & set(step.allowed_tools)
    assert step.allowed_mcp_tools != "all"
    task_tools = {tool for tool in step.allowed_mcp_tools if tool.startswith("gobby-tasks:")}
    assert task_tools == {
        "gobby-tasks:get_task",
        "gobby-tasks:list_tasks",
        "gobby-tasks:search_tasks",
        "gobby-tasks:get_task_diff",
        "gobby-tasks:get_dependency_tree",
    }
    assert "gobby-agents:send_message" in step.allowed_mcp_tools
    assert "gobby-agents:spawn_agent" not in step.allowed_mcp_tools
    assert "the Orchestrator applies every task edit" in prompt
    assert "Never edit tasks, claim work, spawn agents" in prompt
    assert "use the launch assignment and injected project policy" in prompt
    assert "Honor project-policy exclusion roots" in prompt
    assert "exclude all plan-expanded epics and their entire subtrees" in prompt


def test_consolidator_syncs_with_shared_seat_rules(definition_db: PostgresHubDatabase) -> None:
    result = sync_bundled_agents(definition_db)
    assert result["success"] is True
    row = AgentDefinitionManager(definition_db).get_by_name("consolidator")
    assert row is not None
    assert AgentDefinitionBody.model_validate(row.definition_json) == _load("consolidator")
    selected = _selected_rules("consolidator", definition_db)
    assert {"inject-seat-common", "no-send-keys", "block-update-task"} <= selected


def test_root_panes_keep_interactive_git_guards_without_worker_rules(temp_db: HubDatabase) -> None:
    for name in ("assistant", "orchestrator"):
        selected = _selected_rules(name, temp_db)
        assert ROOT_GIT_GUARDS <= selected, name
        assert not RESTART_AND_WORKTREE_RULES & selected, name


def test_spawned_coordination_seats_keep_restart_and_worktree_guards(temp_db: HubDatabase) -> None:
    for name in ("dev-manager", "release-manager", "triage"):
        assert RESTART_AND_WORKTREE_RULES <= _selected_rules(name, temp_db), name


def test_dev_manager_is_read_only_and_reports_to_triage() -> None:
    body = _load("dev-manager")
    prompt = _prompt("dev-manager")

    assert set(body.blocked_tools) == READ_ONLY_BLOCKED_TOOLS
    assert body.task_step_writes == ["closing"]
    # land_commit refuses a session that ever claimed the task.
    assert "never call `claim_task`" in prompt
    assert "Report to Triage" in prompt
    assert "Never message the Orchestrator directly" in prompt
    assert "gobby-agents:wait_for_coordination" in prompt
    for retired in (
        "CANDIDATE_PARKED",
        "CANDIDATE_RESUMED",
        "REVIEW_ONLY_REQUIRED",
        "EVENT=LANDED",
        "CronList",
        "lane",
    ):
        assert retired not in prompt, retired


def test_dev_manager_recovers_stalled_seats_only_without_a_recovery_steward() -> None:
    prompt = _prompt("dev-manager")

    assert "The Recovery Steward owns patrol and recovery of stalled seats" in prompt
    assert "Only when no Recovery Steward is running" in prompt
    assert "`gobby-sessions:send_keys` with literal text plus a trailing newline" in prompt
    assert "`continue: check your queued Gobby messages and proceed\\n`" in prompt
    assert "hand that to the Assistant without reading it" in prompt


def test_release_manager_runs_each_activation_as_a_release_batch() -> None:
    prompt = _prompt("release-manager")

    for heading in ("Landing ledger.", "Release window.", "Activation."):
        assert heading in prompt, heading
    assert "`gobby-agents:acquire_release_window`" in prompt
    assert "Never promote binaries, restart or cut over outside a batch" in prompt
    for retired in ("Close queue.", "record_close_receipt", "task-close-reviewer"):
        assert retired not in prompt, retired


@pytest.mark.parametrize(
    "name",
    (
        "assistant",
        "orchestrator",
        "dev-manager",
        "release-manager",
        "triage",
        "researcher",
        "archivist",
    ),
)
def test_terminal_control_is_opt_in_per_definition(name: str, temp_db: HubDatabase) -> None:
    selected = _selected_rules(name, temp_db)
    assert ("no-send-keys" in selected) is (name not in KEEPS_TERMINAL_CONTROL), name


def test_reporter_owns_bounded_scheduled_delivery() -> None:
    body = _load("reporter")
    prompt = _prompt("reporter")
    assert body.timeout == 900
    assert body.execution_mode == "one_shot"
    assert body.send_message_targets == ["session"]
    assert body.operator_messaging is True
    assert "gobby-communications:send_operator_message" not in body.blocked_mcp_tools
    assert "send_operator_message" in prompt
    assert "resolve_operator_conversation" in prompt
    assert "success=true" in prompt
    assert 'agent_type="assistant"' in prompt
    assert "Do not read log files" in prompt
    assert "300 seconds" in prompt
    assert "cadence and mode selection from the injected project policy" in prompt
    assert "Report mode: full" in prompt
    assert "Report mode: delta" in prompt
    assert 'view="compact", limit=500' in prompt
    assert "accumulated rows equal total" in prompt
    assert "first recorded claim time" in prompt
    assert "gobby.agents.reporter_sources --section queues" in prompt
    assert "never use head" in prompt.lower()
    assert "candidate=true, closed=false" in prompt
    assert "latest_linked_commit" in prompt
    assert "full-report scaffold" in prompt
    assert "Delivery success and report completeness are separate results" in prompt
    assert "closed / escalated" in prompt
    assert "never contacts the operator" not in prompt
    assert "the Assistant does that" not in prompt
    for section in (
        "Policy-defined lanes",
        "time claimed",
        "next five",
        "landing/activation/close",
        "Close backlog",
        "Hold state",
        "Blocked or escalated",
        "Operator decisions",
        "Stalled or idle",
        "Worktrees",
        "Consolidator",
        "ROADMAP",
        "policy-designated critical paths",
        "Last 30 minutes",
        "Systems nominal",
    ):
        assert section in prompt, section
    assert "Heavy slots" not in prompt


def test_reporter_roster_covers_ordered_tabs_empty_panes_and_unclaimed_seats() -> None:
    assert "Worktrees versus developers; Workspace seat roster;" in _prompt("reporter")
    roster = (
        _prompt("reporter").split("## Workspace seat roster", 1)[1].split("## Full report", 1)[0]
    )
    for requirement in (
        "default_project_id",
        "gobby-workspaces:get_workspace",
        "every tab in tab order",
        "every pane under its tab in layout order",
        "terminal_context.gobby_terminal_id",
        "gobby-sessions:list_sessions",
        "gobby-agents:list_running_agents",
        "provider, model, reasoning effort, state",
        "claimed task ref with title (or none)",
        "unlabeled | empty | none / none / none | no seat | task none",
        "idle | task none",
        "working",
        "waiting",
        "stalled",
        "roster_complete",
        "coverage incomplete",
    ):
        assert requirement in roster, requirement
    assert not re.search(r"(?:gobby#|Lane |#\d|[0-9a-f]{8}-[0-9a-f]{4}-)", roster)


def test_reporter_bounds_queue_resolution_and_names_unreadable_rows() -> None:
    prompt = _prompt("reporter")
    budget = _section(prompt, "Cadence and budget")
    sources = _section(prompt, "Sources")
    assert "240 seconds" in budget
    assert "bounded queue refills" in budget
    assert "remain inside the time budget" in budget
    assert "current/next-five/candidate block before gathering supplementary evidence" in budget
    assert "Cache task rows by ref across all sources" in sources
    assert "at most five unresolved queue refs" in sources
    assert 'view="compact", limit=40' in sources
    assert "refill only lanes still short" in sources
    assert "five eligible tasks are found or its queue is exhausted" in sources
    assert "refs resolution ignores filters" in sources
    assert "state object" in sources
    assert 'Never print "state ?"' in sources
    assert "ref_errors, omitted rows or unreadable state" in sources
    assert "GAP: Lane <N> <task ref and known title, or title unavailable>" in sources
    assert "mark next-five coverage incomplete even if later refs fill five slots" in sources


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
    reporter = manager.get_by_name("reporter")
    assert reporter is not None
    assert AgentDefinitionBody.model_validate(reporter.definition_json) == _load("reporter")
    for row in manager.list_all():
        body = AgentDefinitionBody.model_validate(row.definition_json)
        assert body.operator_messaging is (row.name in {"reporter", "assistant", "archivist"}), (
            row.name
        )


def test_installed_reporter_can_send_assistant_fallback(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    result = sync_bundled_agents(temp_db)
    assert result["success"] is True
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        external_id="reporter-fallback",
        machine_id=require_machine_id(),
        source="claude",
        project_id=str(sample_project["id"]),
        agent_depth=1,
    )
    assert send_message_target_allowed(sessions, caller.id, "reporter", "session") is True
    assert send_message_target_allowed(sessions, caller.id, "reporter", "global") is False


def test_triage_is_read_only_and_queues_one_digest_for_the_orchestrator() -> None:
    body = _load("triage")
    prompt = _prompt("triage")

    assert set(body.blocked_tools) == READ_ONLY_BLOCKED_TOOLS
    assert body.network == "none"
    assert body.prewarm_pre_commit_store is False
    assert "Work read-only in the main checkout" in prompt
    assert "Every five minutes, send the Orchestrator ONE digest with wake=false" in prompt
    assert 'single word "steady"' in prompt
    # Urgent items still queue: only the operator's request wakes the Orchestrator.
    assert "Forward at once" in prompt
    assert "Use wake=true only when the operator asks" in prompt
    assert "Never spawn agents, create or update tasks, edit files, restart the daemon" in prompt
    assert "Landing and close decisions belong to the Dev Managers" in prompt
    for retired in (
        "recurring reminder",
        "close backlog",
        "RETEST=",
        "EVENT=LANDED",
        "HOLD or RESUME ACK",
        "lane",
    ):
        assert retired not in prompt, retired
