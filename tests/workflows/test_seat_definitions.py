"""Contract test for the standing seat bundle (plan agent-definition-profiles 3.5).

Every `seat`-tagged bundled definition is checked from the checkout, so the
contract needs no daemon. The plan-writer, plan-enhancer and plan-adversary
runbook agents are run-scoped and stay outside the seat invariants;
tests/agents/test_plan_seat_definitions.py covers them.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.skills.sync import get_bundled_skills_path
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import validate_workflow_definition_data

pytestmark = pytest.mark.unit

AGENTS_DIR = get_bundled_agents_path()
SKILLS_DIR = get_bundled_skills_path()

SEAT_CATALOGUE = {
    "assistant",
    "orchestrator",
    "lane-manager",
    "developer",
    "code-reviewer",
    "researcher",
    "archivist",
    "log-monitor",
    # #23752: the standing Inbox and Merge Manager seats.
    "inbox-manager",
    "merge-manager",
}
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
READ_ONLY_SEATS = {"lane-manager", "code-reviewer", "log-monitor", "researcher", "inbox-manager"}
WRITE_SCOPE_RULES = {"assistant": "assistant-write-scope", "archivist": "archivist-write-scope"}
STEP_SEATS = {"developer", "code-reviewer", "log-monitor", "researcher"}

SYNC_METADATA_KEYS = {"tags", "priority", "type"}
UNSUPPORTED_KEYS = {"terminal_backend", "backend", "sandbox"}

SEND_MESSAGE_TARGETS = {
    "assistant": ["parent", "session", "project"],
    "orchestrator": ["parent", "session", "project", "global"],
    "lane-manager": ["parent", "session", "project"],
    "developer": ["parent", "session"],
    "code-reviewer": ["parent", "session"],
    "researcher": ["parent", "session"],
    "archivist": ["parent", "session"],
    "log-monitor": ["parent", "session"],
    "inbox-manager": ["parent", "session"],
    "merge-manager": ["parent", "session", "project"],
}
ORCHESTRATOR_INCLUDES = {
    "tag:worker-safety",
    "name:no-force-push-interactive",
    "name:no-destructive-git-interactive",
}
ORCHESTRATOR_EXCLUDES = [
    "name:no-daemon-management",
    "name:no-daemon-management-http",
    "name:block-git-worktree-mutations",
]

# Steps that wait on another session report and wait from the step itself.
# #23750 adds the developer and code-reviewer load_skills and the developer
# claim reports, so a relaunched seat can reach its Lane Manager before work.
WAITING_STEPS = {
    "developer": {"load_skills", "claim", "submit"},
    "code-reviewer": {"load_skills", "await", "verdict"},
    "log-monitor": {"tick", "report"},
    "researcher": {"serve"},
}
# #23750: the steps a relaunched seat bootstraps its role file from.
ROLE_BOOTSTRAP_STEPS = {
    "developer": {"load_skills", "claim"},
    "code-reviewer": {"load_skills", "await"},
}
REPORT_TOOLS = {"gobby-agents:send_message", "gobby-agents:wait_for_coordination"}

LOAD_SKILLS_WHEN = "all(skill_loaded(skill) for skill in vars.required_skills)"
HANDLER_KEYS = ("on_enter", "on_mcp_success", "on_mcp_error", "on_exit")
FLAG_READ_RE = re.compile(r"vars\.(?:get\(\s*['\"])?(\w+)")

ROUTED_SKILLS = {"impeccable", "rust", "typescript", "tech-writer"}
CANDIDATE_EVENT_LINE = "`LANE= EVENT=CANDIDATE TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE=`"
ROUTE_LINE_RE = re.compile(r"^- [^:]+: (?P<skills>.+)$")


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
        if isinstance(default, bool) and name not in raised
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
def test_seat_definitions_share_the_seat_contract(name: str) -> None:
    seat = _bundled()[name]
    prompt = _prompt(seat)

    assert seat["surfaces"] == ["spawn", "persona"]
    assert seat["prompts"]["persona"] == prompt
    assert seat["provider"] == "inherit"
    assert "model" not in seat
    assert seat["checkout_mode"] == "inherit"
    assert seat["timeout"] == 0
    assert set(seat) <= set(AgentDefinitionBody.model_fields) | SYNC_METADATA_KEYS
    assert not set(seat) & UNSUPPORTED_KEYS
    assert validate_workflow_definition_data(seat, expected_type="agent") == "agent"
    assert seat["send_message_targets"] == SEND_MESSAGE_TARGETS[name]

    selectors = _selectors(seat)
    include = selectors.get("include") or []
    assert "tag:roles" in include
    assert not any("review-learning" in entry for value in selectors.values() for entry in value)
    if name in {"assistant", "lane-manager"}:
        assert "tag:worker-safety" in include
    if name == "orchestrator":
        assert ORCHESTRATOR_INCLUDES <= set(include)
        assert selectors["exclude"] == ORCHESTRATOR_EXCLUDES

    assert "## Platform Context" in prompt
    assert "## Skills" in prompt
    assert "gobby-agents:send_message" in prompt
    assert "lesson" not in prompt.lower()

    blocked = set(seat.get("blocked_tools") or [])
    if name in READ_ONLY_SEATS:
        assert blocked == READ_ONLY_BLOCKED_TOOLS
    if name in WRITE_SCOPE_RULES:
        assert not blocked & READ_ONLY_BLOCKED_TOOLS
        assert WRITE_SCOPE_RULES[name] in prompt

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
