"""Contract test for the standing seat bundle (plan agent-definition-profiles 3.5).

Every `seat`-tagged bundled definition is checked from the checkout, so the
contract needs no daemon. The plan-writer, plan-enhancer and plan-adversary
runbook agents are run-scoped and stay outside the seat invariants;
tests/agents/test_plan_seat_definitions.py covers them.
"""

from __future__ import annotations

import re
from functools import cache
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.skills.formatting import skill_fetch_directive
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.skills.sync import get_bundled_skills_path
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import validate_workflow_definition_data
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

AGENTS_DIR = get_bundled_agents_path()
SKILLS_DIR = get_bundled_skills_path()
SEAT_GATES = AGENTS_DIR.parent / "rules/skill-discovery/require-seat-procedure.yaml"
GCLIENT_REFERENCE = "gobby:references/agents/gclient.md"
SEAT_REFERENCE_EXEMPT_TOOLS = [
    ("gobby", "get_tool_schema"),
    ("gobby", "get_variable"),
    ("gobby-skills", "get_skill_file"),
    ("gobby-results", "get_tool_result"),
    ("gobby-sessions", "get_handoff"),
    ("gobby-sessions", "set_handoff"),
    ("gobby-memory", "get_memory"),
    ("gobby-agents", "send_message"),
    ("gobby-agents", "wait_for_coordination"),
]

SEAT_CATALOGUE = {
    "assistant",
    "orchestrator",
    "dev-manager",
    "test-writer",
    "developer",
    "code-reviewer",
    "researcher",
    "archivist",
    "log-monitor",
    "metrics-monitor",
    "triage",
    "release-manager",
    "reporter",
    "security-reviewer",
    "debugger",
    "design-reviewer",
    "recovery-steward",
    "consolidator",
    "docs-reviewer",
    "research-reviewer",
}
# Root panes: the operator opens them, so they never take the spawn surface.
PERSONA_ONLY_SEATS = {"assistant", "orchestrator"}
# A short-lived spawned run: no persona surface, a bounded one-shot run.
SPAWN_ONLY_SEATS = {"reporter"}
# Seats whose definitions leave `name:no-send-keys` out of the rule selection.
SEND_KEYS_SEATS = {"default", "assistant", "orchestrator", "dev-manager", "recovery-steward"}
RUNBOOK_AGENTS = ("plan-writer", "plan-enhancer", "plan-adversary")

# Decision 8: the runbooks list every read-only seat blocks.
READ_ONLY_BLOCKED_TOOLS = {
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
READ_ONLY_SEATS = {
    "dev-manager",
    "log-monitor",
    "metrics-monitor",
    "triage",
    "reporter",
    "security-reviewer",
    "debugger",
    "design-reviewer",
    "recovery-steward",
    "consolidator",
}
WRITE_SCOPE_RULES = {"assistant": "assistant-write-scope", "archivist": "archivist-write-scope"}
STEP_SEATS = {
    "developer",
    "code-reviewer",
    "log-monitor",
    "metrics-monitor",
    "researcher",
    "consolidator",
    "docs-reviewer",
    "research-reviewer",
}

SYNC_METADATA_KEYS = {"tags", "priority", "type"}
UNSUPPORTED_KEYS = {"terminal_backend", "backend", "sandbox"}

SEND_MESSAGE_TARGETS = {
    "assistant": ["parent", "session", "agent", "project", "global"],
    "orchestrator": ["parent", "session", "agent", "project", "global"],
    "dev-manager": ["parent", "session", "project"],
    "test-writer": ["parent", "session"],
    "developer": ["parent", "session"],
    "code-reviewer": ["parent", "session"],
    "researcher": ["parent", "session"],
    "archivist": ["parent", "session"],
    "log-monitor": ["parent", "session"],
    "metrics-monitor": ["parent", "session"],
    "triage": ["parent", "session"],
    "release-manager": ["parent", "session", "project", "global"],
    "reporter": ["session"],
    "security-reviewer": ["parent", "session"],
    "debugger": ["parent", "session"],
    "design-reviewer": ["parent", "session"],
    "recovery-steward": ["parent", "session"],
    "consolidator": ["parent", "session"],
    "docs-reviewer": ["parent", "session"],
    "research-reviewer": ["parent", "session"],
}
# The Assistant and Orchestrator are root panes under the interactive rules.
ROOT_PANE_INCLUDES = ["tag:default"]
ROOT_PANE_EXCLUDES = ["name:block-update-task"]

# Steps that wait on another session report and wait from the step itself.
# #23750 adds the developer and code-reviewer load_skills and the developer
# claim reports, so a relaunched seat can reach its Dev Manager before work.
WAITING_STEPS = {
    "developer": {"load_skills", "claim", "submit"},
    "code-reviewer": {"load_skills", "await", "review", "verdict", "fix"},
    "log-monitor": {"tick", "report"},
    "metrics-monitor": {"tick", "report"},
    "researcher": {"serve"},
    "consolidator": {"load_skills", "consolidate"},
    "docs-reviewer": {"load_skills", "await", "review", "verdict"},
    "research-reviewer": {"load_skills", "await", "review", "verdict"},
}
# #23750: the steps a relaunched seat bootstraps its role file from.
ROLE_BOOTSTRAP_STEPS = {
    "developer": {"load_skills", "claim"},
    "code-reviewer": {"load_skills", "await"},
    "docs-reviewer": {"load_skills", "await"},
    "research-reviewer": {"load_skills", "await"},
}
REPORT_TOOLS = {"gobby-agents:send_message", "gobby-agents:wait_for_coordination"}

LOAD_SKILLS_WHEN = "all(skill_loaded(skill) for skill in vars.required_skills)"
HANDLER_KEYS = ("on_enter", "on_mcp_success", "on_mcp_error", "on_exit")
FLAG_READ_RE = re.compile(r"vars\.(?:get\(\s*['\"])?(\w+)")

ROUTED_SKILLS = {"impeccable", "rust", "typescript", "tech-writer"}
CANDIDATE_EVENT_LINE = "`EVENT=CANDIDATE TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE=`"
ROUTE_LINE_RE = re.compile(r"^- [^:]+: (?P<skills>.+)$")
# Seat flags the workflow engine writes itself (seat_transitions.py). The
# native-variable-authority rule rejects hand writes to them, so no step needs set_variable.
ENGINE_OWNED_FLAGS = frozenset(
    {
        "bounced",
        "candidate_received",
        "consensus",
        "implementation_complete",
        "log_monitor_reported",
        "skills_routed",
        "tick_done",
        "triage_reported",
        "verdict_ready",
    }
)


@cache
def _bundled() -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for path in sorted(AGENTS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, dict), path
        definitions[str(data["name"])] = data
    return definitions


def _seat_names() -> list[str]:
    return sorted(name for name, data in _bundled().items() if "seat" in (data.get("tags") or []))


def _prompt(seat: dict[str, Any]) -> str:
    prompt = seat["prompts"]["agent"]
    assert isinstance(prompt, str)
    return prompt


def _section(prompt: str, heading: str) -> str:
    start = prompt.index(f"## {heading}\n")
    end = prompt.find("\n## ", start + 1)
    return prompt[start : end if end != -1 else len(prompt)]


def _selectors(seat: dict[str, Any]) -> dict[str, list[str]]:
    selectors = (seat.get("workflows") or {}).get("rule_selectors") or {}
    assert isinstance(selectors, dict)
    return selectors


def _lists(step: dict[str, Any], key: str, entry: str) -> bool:
    values = step.get(key, "all")
    return values == "all" or entry in values


def _flags_set_by_hand(workflow: dict[str, Any]) -> set[str]:
    """Boolean flags that no step handler raises, so only a set_variable call can."""
    raised = {
        handler["variable"]
        for step in workflow["steps"]
        for key in HANDLER_KEYS
        for handler in step.get(key) or []
        if handler.get("action") == "set_variable" and handler.get("value") is not False
    }
    return {
        name
        for name, default in (workflow.get("variables") or {}).items()
        if isinstance(default, bool) and name not in raised and name not in ENGINE_OWNED_FLAGS
    }


def _assert_step_workflow(name: str, seat: dict[str, Any]) -> None:
    workflow = seat.get("step_workflow")
    if name not in STEP_SEATS:
        assert workflow is None, name
        return
    assert workflow is not None, name
    assert workflow.get("exit_condition") is None, name
    steps = {step["name"]: step for step in workflow["steps"]}

    first = workflow["steps"][0]
    assert first["name"] == "load_skills", name
    assert [entry["when"] for entry in first["transitions"]] == [LOAD_SKILLS_WHEN], name

    hand_set = _flags_set_by_hand(workflow)
    for step in workflow["steps"]:
        read = {
            flag
            for entry in step.get("transitions") or []
            for flag in FLAG_READ_RE.findall(entry["when"])
        }
        if read & hand_set:
            assert _lists(step, "allowed_tools", "mcp__gobby__set_variable"), (name, step["name"])

    for step_name in WAITING_STEPS[name]:
        for tool in sorted(REPORT_TOOLS):
            assert _lists(steps[step_name], "allowed_mcp_tools", tool), (name, step_name, tool)
    for step_name in ROLE_BOOTSTRAP_STEPS.get(name, ()):
        for tool in ("Bash", "Read"):
            assert _lists(steps[step_name], "allowed_tools", tool), (name, step_name, tool)


@pytest.mark.parametrize("name", _seat_names())
def test_seat_run_lifetime(name: str) -> None:
    seat = _bundled()[name]
    body = AgentDefinitionBody.model_validate(seat)
    assert body.execution_mode == ("one_shot" if name in SPAWN_ONLY_SEATS else "interactive")
    if name == "researcher":
        assert body.idle_ttl_seconds == 900
    else:
        assert body.idle_ttl_seconds is None
        assert "idle_ttl_seconds" not in seat


@pytest.mark.parametrize("name", _seat_names())
def test_seat_definitions_share_the_seat_contract(name: str) -> None:
    seat = _bundled()[name]
    prompt = _prompt(seat)

    if name in PERSONA_ONLY_SEATS:
        assert seat["surfaces"] == ["persona"]
    elif name in SPAWN_ONLY_SEATS:
        assert seat["surfaces"] == ["spawn"]
    else:
        assert seat["surfaces"] == ["spawn", "persona"]
    if "persona" in seat["surfaces"]:
        assert seat["prompts"]["persona"] == prompt
    else:
        assert "persona" not in seat["prompts"]
    assert seat["provider"] == "inherit"
    # Launch defaults choose every seat's model; Triage keeps its own reasoning effort.
    assert "model" not in seat
    if name == "triage":
        assert seat["reasoning_effort"] == "medium"
    else:
        assert "reasoning_effort" not in seat
    assert seat["checkout_mode"] == "inherit"
    assert seat["timeout"] == (900 if name in SPAWN_ONLY_SEATS else 0)
    assert set(seat) <= set(AgentDefinitionBody.model_fields) | SYNC_METADATA_KEYS
    assert not set(seat) & UNSUPPORTED_KEYS
    assert validate_workflow_definition_data(seat, expected_type="agent") == "agent"
    assert seat["send_message_targets"] == SEND_MESSAGE_TARGETS[name]

    selectors = _selectors(seat)
    include = selectors.get("include") or []
    assert "tag:roles" not in include
    assert ("name:no-send-keys" in include) is (name not in SEND_KEYS_SEATS)
    assert not any("review-learning" in entry for value in selectors.values() for entry in value)
    if name in ("dev-manager", "test-writer"):
        assert "tag:worker-safety" in include
    if name in PERSONA_ONLY_SEATS:
        assert include == ROOT_PANE_INCLUDES
        assert selectors["exclude"] == ROOT_PANE_EXCLUDES

    reference = (seat.get("workflows") or {}).get("variables", {}).get("_seat_procedure")
    if reference:
        assert reference in prompt
        path = get_bundled_agents_path().parents[1] / "skills/gobby" / reference.split(":", 1)[1]
        prompt += "\n" + path.read_text()
    assert "## Platform Context" in prompt
    assert "## Skills" in prompt
    assert "gobby-agents:send_message" in prompt
    # Only the Dev Manager records lessons, in its closing step record.
    assert ("lesson" in prompt.lower()) is (name == "dev-manager")

    blocked = set(seat.get("blocked_tools") or [])
    if name in READ_ONLY_SEATS:
        assert blocked == READ_ONLY_BLOCKED_TOOLS
    if name == "code-reviewer":
        # Review steps stay read-only by step; the fix step edits after a claim hand-off.
        assert blocked == {"KillShell"}
    if name in WRITE_SCOPE_RULES:
        assert not blocked & READ_ONLY_BLOCKED_TOOLS
        assert WRITE_SCOPE_RULES[name] in prompt
    if name == "researcher":
        assert blocked == {"KillShell"}
        assert "researcher-write-scope" in prompt
        assert ".gobby/plans/research/" in prompt

    _assert_step_workflow(name, seat)


def test_required_skills_resolve_to_bundled_skills() -> None:
    declaring: set[str] = set()
    for name, data in _bundled().items():
        workflow = data.get("step_workflow") or {}
        for entry in (workflow.get("variables") or {}).get("required_skills") or []:
            requirement = parse_instruction_requirement(entry)
            skill_dir = SKILLS_DIR / requirement.skill
            assert (skill_dir / "SKILL.md").is_file(), (name, entry)
            if requirement.path is not None:
                assert (skill_dir / requirement.path).is_file(), (name, entry)
            declaring.add(name)
    assert STEP_SEATS | {"plan-adversary"} <= declaring


def test_seat_catalogue_is_exact() -> None:
    assert set(_seat_names()) == SEAT_CATALOGUE
    for name in RUNBOOK_AGENTS:
        assert "seat" not in (_bundled()[name].get("tags") or []), name


def test_archivist_sends_workspace_digest_as_attachment() -> None:
    prompt = _prompt(_bundled()["archivist"])
    assert "<project>/.gobby/reports/gobby-digest-YYYY-MM-DD.md" in prompt
    assert "literal absolute path" in prompt
    assert "gobby-communications:send_operator_attachment" in prompt
    assert (
        "Resolve the operator channel from project policy or communications configuration" in prompt
    )
    assert "resolve_operator_conversation" in prompt
    assert _bundled()["archivist"]["operator_messaging"] is True
    assert "at most a one-line caption" in prompt


def test_developer_routes_skills_by_task() -> None:
    prompt = _prompt(_bundled()["developer"])
    assert CANDIDATE_EVENT_LINE in _section(prompt, "Role")

    routed: set[str] = set()
    for line in _section(prompt, "Task skills").splitlines():
        match = ROUTE_LINE_RE.match(line)
        if match is not None:
            routed.update(re.findall(r"`([^`]+)`", match["skills"]))
    assert ROUTED_SKILLS <= routed
    for skill in routed:
        assert (SKILLS_DIR / skill / "SKILL.md").is_file(), skill


@pytest.mark.parametrize("name", ["orchestrator", "assistant", "dev-manager"])
def test_seat_references_gate_required_reading(name: str) -> None:
    raw = yaml.safe_load(SEAT_GATES.read_text())
    rule = raw["rules"]["require-seat-references"]
    assert rule["enabled"] and rule["event"] == "before_tool"
    [effect] = rule["effects"]
    assert effect["type"] == "block"

    seat = _bundled()[name]
    assert GCLIENT_REFERENCE in seat["workflows"]["variables"]["_seat_references"]
    assert "tag:default" in _selectors(seat)["include"] and "default" in raw["tags"]
    content = (SKILLS_DIR / "gobby" / GCLIENT_REFERENCE.split(":", 1)[1]).read_text()
    assert len(content) < 15_000 and "{{" not in content and "{%" not in content
    for topic in ("tab_title", "capture_output", "send_keys", "project policy"):
        assert topic in content, topic

    procedure = "gobby:references/agents/procedures/orchestrator.md"
    variables: dict[str, Any] = {
        "_seat_references": [procedure, GCLIENT_REFERENCE],
        "loaded_skill_references": [],
    }
    event = SimpleNamespace(data={"tool_name": "Bash", "tool_input": {"command": "git status"}})
    context = {"variables": variables, "event": event, "tool_input": event.data["tool_input"]}
    helpers = build_condition_helpers(context=context)
    evaluator = SafeExpressionEvaluator(context, helpers)

    def reason() -> str:  # The engine adds skill_fetch_directive to the condition helpers.
        funcs = {**helpers, "skill_fetch_directive": skill_fetch_directive}
        return TemplateEngine(strict_undefined=False).render(effect["reason"], {**context, **funcs})

    assert evaluator.evaluate(rule["when"]), "Work must wait for every listed reference"
    assert reason() == skill_fetch_directive(procedure)
    variables["loaded_skill_references"] = [procedure]
    assert evaluator.evaluate(rule["when"]), "One loaded reference cannot release the rest"
    assert reason() == skill_fetch_directive(GCLIENT_REFERENCE)
    variables["loaded_skills"] = [GCLIENT_REFERENCE]
    assert evaluator.evaluate(rule["when"]), "A skill entrypoint cannot replace its reference"
    variables["loaded_skill_references"] = [procedure, GCLIENT_REFERENCE]
    assert not evaluator.evaluate(rule["when"])
    variables["loaded_skill_references"] = []  # Context loss clears tracked reference loads.
    assert evaluator.evaluate(rule["when"]), "Compaction must require the references again"
    for server, tool in SEAT_REFERENCE_EXEMPT_TOOLS:
        event.data = {"mcp_server": server, "mcp_tool": tool}
        assert not evaluator.evaluate(rule["when"]), f"Recovery must remain available: {tool}"
    event.data = {"mcp_server": "gobby-agents", "mcp_tool": "spawn_agent"}
    assert evaluator.evaluate(rule["when"]), "Pane placement must wait for the reference"
    del variables["_seat_references"]
    assert not evaluator.evaluate(rule["when"]), "Definitions without the list stay ungated"
