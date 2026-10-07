"""Contract tests for the review and observation seat definitions.

The code-reviewer, archivist, log-monitor and researcher seats are bundled
definitions whose prompt is the role text of record (agent-definition-profiles
P3, section 3.3). They share the seat shape and differ in their tool
restrictions, step workflows and message contracts. The lane-manager relay of
the reviewer's landing lines ships here too, because sender and relay are one
protocol.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.definitions import AgentDefinitionBody, WorkflowStep
from gobby.workflows.safe_evaluator import (
    SafeExpressionEvaluator,
    build_agent_workflow_allowed_funcs,
)
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

SEATS = ("code-reviewer", "archivist", "log-monitor", "researcher")
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
READ_ONLY_SEATS = ("code-reviewer", "log-monitor", "researcher")
# Sync metadata keys `validate_workflow_definition_data` strips before the body.
SYNC_METADATA_KEYS = frozenset({"tags", "priority", "type"})
OBSERVATION_SELECTORS = frozenset(
    {
        "tag:default",
        "tag:roles",
        "tag:context-handoff",
        "tag:memory-lifecycle",
        "tag:worker-safety",
    }
)
RULE_SELECTORS = {
    # Josh's hold (memory 62552350): never tag:review-learning.
    "code-reviewer": frozenset({"tag:default", "tag:roles"}),
    "archivist": OBSERVATION_SELECTORS,
    "log-monitor": OBSERVATION_SELECTORS,
    "researcher": OBSERVATION_SELECTORS | {"tag:task-skill-gates"},
}
# Decision 10: continuity is prose.
COMPACT_SEATS = ("archivist", "log-monitor")
CLEAR_SEATS = ("code-reviewer", "researcher")
STEP_SEATS = ("code-reviewer", "log-monitor", "researcher")
LOAD_SKILLS_WHEN = "all(skill_loaded(skill) for skill in vars.required_skills)"


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


def _variables(name: str) -> dict[str, Any]:
    step_workflow = _load(name).step_workflow
    assert step_workflow is not None, name
    assert step_workflow.exit_condition is None, name
    return step_workflow.variables


def _steps(name: str) -> dict[str, WorkflowStep]:
    step_workflow = _load(name).step_workflow
    assert step_workflow is not None, name
    return {step.name: step for step in step_workflow.steps}


def _transitions(step: WorkflowStep) -> list[tuple[str, str]]:
    return [(transition.to, transition.when) for transition in step.transitions]


def _allowed_tools(step: WorkflowStep) -> set[str]:
    assert isinstance(step.allowed_tools, list), step.name
    return set(step.allowed_tools)


def _allowed_mcp_tools(step: WorkflowStep) -> set[str]:
    assert isinstance(step.allowed_mcp_tools, list), step.name
    return set(step.allowed_mcp_tools)


def _send_message_resets(step: WorkflowStep) -> dict[str, dict[str, Any]]:
    """The step's ``send_message`` success handlers, keyed by the variable each sets."""
    return {
        handler["variable"]: handler
        for handler in step.on_mcp_success
        if (handler["server"], handler["tool"]) == ("gobby-agents", "send_message")
    }


def _matches(condition: str, content: str, **variables: Any) -> bool:
    context: dict[str, Any] = {"vars": variables, "tool_input": {"content": content}}
    evaluator = SafeExpressionEvaluator(context, build_agent_workflow_allowed_funcs(context))
    return bool(evaluator.evaluate(condition))


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
    assert set(body.workflows.rule_selectors.include) == RULE_SELECTORS[name]
    assert body.workflows.variables == {}
    assert body.blocked_mcp_tools == ["gobby-agents:kill_agent"]
    assert set(body.send_message_targets) == {"parent", "session"}
    for section in _baseline_sections():
        assert section in prompt
    assert "gobby-agents:send_message" in prompt
    assert "lesson" not in prompt.lower()


@pytest.mark.parametrize("name", READ_ONLY_SEATS)
def test_read_only_seats_block_every_write_tool(name: str) -> None:
    assert set(_load(name).blocked_tools) == READ_ONLY_BLOCKED_TOOLS


@pytest.mark.parametrize("name", COMPACT_SEATS)
def test_standing_seats_compact_and_never_clear(name: str) -> None:
    prompt = _prompt(name)

    assert "Continuity: compact, never /clear." in prompt
    assert "set_handoff(clear_session=false)" in prompt


@pytest.mark.parametrize("name", CLEAR_SEATS)
def test_deliverable_seats_clear_between_deliverables(name: str) -> None:
    prompt = _prompt(name)

    assert "set_handoff(clear_session=true)" in prompt
    assert "never /clear" not in prompt


@pytest.mark.parametrize("name", STEP_SEATS)
def test_step_seats_open_by_loading_their_required_skills(name: str) -> None:
    load_skills = next(iter(_steps(name).values()))

    assert load_skills.name == "load_skills"
    assert _allowed_mcp_tools(load_skills) == {
        "gobby-skills:get_skill",
        "gobby-skills:get_skill_file",
    }
    assert _transitions(load_skills)[0][1] == LOAD_SKILLS_WHEN
    assert _variables(name)["required_skills_loaded"] is False


def test_code_reviewer_runs_the_await_review_verdict_loop() -> None:
    variables = _variables("code-reviewer")
    steps = _steps("code-reviewer")
    prompt = _prompt("code-reviewer")

    assert variables["required_skills"] == ["code-review", "restraint"]
    assert {key: variables[key] for key in ("candidate_received", "verdict_ready")} == {
        "candidate_received": False,
        "verdict_ready": False,
    }
    assert variables["candidate_task"] is None
    assert list(steps) == ["load_skills", "await", "review", "verdict"]
    assert _transitions(steps["load_skills"]) == [("await", LOAD_SKILLS_WHEN)]
    assert _transitions(steps["await"]) == [("review", "vars.candidate_received")]
    assert _transitions(steps["review"]) == [("verdict", "vars.verdict_ready")]
    assert _transitions(steps["verdict"]) == [("await", "not vars.candidate_received")]
    assert {"gobby-agents:send_message", "gobby-agents:wait_for_coordination"} <= (
        _allowed_mcp_tools(steps["await"])
    )
    assert "mcp__gobby__set_variable" in _allowed_tools(steps["await"])
    assert {"Bash", "Read", "Grep", "mcp__gobby__set_variable"} <= _allowed_tools(steps["review"])
    assert 'set_variable(name="candidate_task"' in prompt
    assert 'scope="step"' in prompt
    assert "against the current `0.5.0` head" in prompt
    assert "EVENT=CANDIDATE_VERDICT TASK=#NNNNN" in prompt
    assert "VERDICT=LAND|BOUNCE" in prompt
    assert "HIGH, MEDIUM or LOW" in prompt
    assert "`gcode evidence`" in prompt


def test_code_reviewer_lands_its_verdicts_and_reports_the_landing() -> None:
    verdict = _steps("code-reviewer")["verdict"]
    prompt = _prompt("code-reviewer")

    assert _allowed_mcp_tools(verdict) == {
        "gobby-agents:send_message",
        "gobby-tasks:record_close_receipt",
        "gobby-tasks-ops:land_commit",
        "gobby-agents:wait_for_coordination",
    }
    assert {"mcp__gobby__set_variable", "Bash"} <= _allowed_tools(verdict)
    receipt = prompt.index('kind="independent_review_approval"')
    assert receipt < prompt.index("gobby-tasks-ops:land_commit(task_id")
    assert "`retest_required`" in prompt
    assert "EVENT=LAND_PENDING TASK=#NNNNN" in prompt
    assert "until a `receipt_id` returns" in prompt
    assert "`notification_pending`" in prompt
    assert "`Landed #N ...` line" in prompt
    assert (
        "EVENT=LANDED TASK=#NNNNN TASK_TITLE= SHA= BRANCH= RECEIPT= MODE= LANDED_TIP= "
        "ACTIVATION= PROVENANCE= RETEST=PASS|FAIL|UNAVAILABLE|NOT_REQUIRED NOTE="
    ) in prompt
    assert "EVENT=LAND_BLOCKED TASK=#NNNNN" in prompt
    # land_commit refuses a new SHA with review_receipt_missing until it has its own receipt.
    new_sha = prompt.index("Review a new SHA for the same task")
    assert "record a fresh `independent_review_approval`" in prompt[new_sha:]
    assert "Merge Manager" not in prompt


@pytest.mark.parametrize(
    ("content", "resets"),
    [
        ("EVENT=CANDIDATE_VERDICT TASK=#42 SHA=abc VERDICT=BOUNCE NOTE=HIGH", True),
        ("EVENT=LANDED TASK=#42 SHA=abc RECEIPT=r1 RETEST=PASS", True),
        ("EVENT=CANDIDATE_VERDICT TASK=#42 SHA=abc VERDICT=LAND", False),
        ("EVENT=LAND_PENDING TASK=#42 SHA=abc NOTE=receipt_pending", False),
        ("EVENT=LAND_BLOCKED TASK=#42 SHA=abc NOTE=merge_conflict", False),
        ("EVENT=LANDED TASK=#43 SHA=abc RECEIPT=r1", False),
    ],
    ids=["bounce", "landed", "land-verdict", "land-pending", "land-blocked", "other-task"],
)
def test_code_reviewer_resets_only_on_a_bounce_or_landing_for_its_candidate(
    content: str, resets: bool
) -> None:
    handlers = _send_message_resets(_steps("code-reviewer")["verdict"])

    assert {variable: handler["value"] for variable, handler in handlers.items()} == {
        "candidate_received": False,
        "verdict_ready": False,
        "candidate_task": None,
    }
    for handler in handlers.values():
        assert _matches(handler["when"], content, candidate_task="#42") is resets


def test_archivist_writes_only_the_desktop_digest() -> None:
    body = _load("archivist")
    prompt = _prompt("archivist")

    assert body.blocked_tools == []
    assert body.step_workflow is None
    assert "`archivist-write-scope`" in prompt
    assert "`~/Desktop/gobby-digest-YYYY-MM-DD.md`" in prompt
    assert "No one else writes the digest" in prompt
    assert '"Lane queues"' in prompt
    assert "only active and parked" in prompt
    assert "Carries forward" in prompt
    assert "only from Orchestrator evidence" in prompt
    assert "Flag gaps or contradictions to the Orchestrator" in prompt


def test_log_monitor_runs_the_tick_report_loop() -> None:
    variables = _variables("log-monitor")
    steps = _steps("log-monitor")
    prompt = _prompt("log-monitor")

    assert variables["required_skills"] == [
        "brevity",
        "gobby:references/observability/diagnostics.md",
        "gobby:references/observability/metrics.md",
    ]
    assert variables["tick_done"] is False
    assert variables["tick_window"] is None
    assert list(steps) == ["load_skills", "tick", "report"]
    assert _transitions(steps["load_skills"]) == [("tick", LOAD_SKILLS_WHEN)]
    assert _transitions(steps["tick"]) == [("report", "vars.tick_done")]
    assert _transitions(steps["report"]) == [("tick", "not vars.tick_done")]
    assert "gobby-agents:wait_for_coordination" in _allowed_mcp_tools(steps["tick"])
    assert {"gobby-agents:send_message", "gobby-agents:wait_for_coordination"} <= (
        _allowed_mcp_tools(steps["report"])
    )
    for step in ("tick", "report"):
        assert "mcp__gobby__set_variable" in _allowed_tools(steps[step])
    assert "reply=true, timeout=600" in prompt
    assert 'set_variable(name="tick_window"' in prompt
    assert (
        "Systems nominal | window HH:MM-HH:MM | <N> warnings in <K> families, all mapped"
    ) in prompt
    assert "EVENT=ALARM" in prompt
    assert "Copy every ALARM to the Assistant" in prompt
    assert "matched time windows" in prompt
    assert "~/.gobby/logs/" in prompt
    assert "log-monitor-tick" not in prompt


@pytest.mark.parametrize(
    ("content", "resets"),
    [
        ("Systems nominal | window 14:00-14:10 | 3 warnings in 2 families, all mapped", True),
        ("EVENT=ALARM WINDOW=14:00-14:10 NOTE=hooks.log errors", True),
        ("Status: tick in progress for 14:00-14:10", False),
    ],
    ids=["nominal", "alarm", "status-reply"],
)
def test_log_monitor_resets_only_on_a_nominal_or_alarm_report(content: str, resets: bool) -> None:
    handlers = _send_message_resets(_steps("log-monitor")["report"])

    assert {variable: handler["value"] for variable, handler in handlers.items()} == {
        "tick_done": False,
        "tick_window": None,
    }
    for handler in handlers.values():
        assert _matches(handler["when"], content) is resets


def test_researcher_is_a_research_only_serving_seat() -> None:
    variables = _variables("researcher")
    steps = _steps("researcher")
    prompt = _prompt("researcher")

    # Absorbed #23003: a sandboxed researcher reaches the web through the trusted allowlist.
    assert _load("researcher").network == "trusted"
    assert variables["required_skills"] == ["research", "restraint", "brevity"]
    assert list(steps) == ["load_skills", "serve"]
    assert _transitions(steps["load_skills"]) == [("serve", LOAD_SKILLS_WHEN)]
    assert steps["serve"].transitions == []
    assert {"gobby-agents:send_message", "gobby-agents:wait_for_coordination"} <= (
        _allowed_mcp_tools(steps["serve"])
    )
    assert "claim" not in steps
    assert "Research only" in prompt
    assert "to the Orchestrator" in prompt
    assert "through the Assistant" in prompt
    assert "Ask the Orchestrator before killing processes" in prompt


def test_lane_manager_relays_reviewer_landings_and_gates_the_close_release() -> None:
    body = _load("lane-manager")
    prompt = _prompt("lane-manager")

    assert body.step_workflow is None
    assert "Merge Manager" not in prompt
    for event in ("EVENT=LANDED", "EVENT=LAND_PENDING", "EVENT=LAND_BLOCKED"):
        assert event in prompt
    assert "Relay each of those lines to the Orchestrator" in prompt
    assert "`EVENT=LANDED` line that carries a `RECEIPT` value" in prompt
    assert "git merge-base --is-ancestor <LANDED_TIP> <BRANCH>" in prompt
    assert "`MODE=merge` landing must carry `RETEST=PASS`" in prompt
    assert "`RETEST=FAIL` or `RETEST=UNAVAILABLE` withholds the release" in prompt
    assert "`ACTIVATION=unknown` withholds the release" in prompt
    assert "your release alone admits a close" in prompt


def test_review_and_observation_seats_sync_as_installed_rows(
    definition_db: PostgresHubDatabase,
) -> None:
    result = sync_bundled_agents(definition_db)

    assert result["success"] is True
    assert result["errors"] == []
    manager = AgentDefinitionManager(definition_db)
    for name in SEATS:
        row = manager.get_by_name(name)
        assert row is not None, name
        assert row.enabled is True, name
        assert AgentDefinitionBody.model_validate(row.definition_json) == _load(name), name
