"""Contract tests for the review and steward seat definitions.

security-reviewer, design-reviewer, debugger and recovery-steward are standing
seats. reporter is a short-lived run that only spawns. Every definition is loaded
through the AgentDefinitionBody model from the bundled checkout, and the
no-send-keys selection is resolved against synced rule rows.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import validate_workflow_definition_data
from gobby.workflows.selectors import rule_matches_agent
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

STANDING_SEATS = ("security-reviewer", "design-reviewer", "debugger", "recovery-steward")
NEW_SEATS = (*STANDING_SEATS, "reporter")
# Copy of the write-tool block in lane-manager.yaml.
WRITE_BLOCKED_TOOLS = frozenset(
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
# Sections every new seat copies from release-manager.yaml; ## Role is seat-specific.
COPIED_HEADINGS = ("## Messages", "## Continuity", "## Platform Context", "## Skills")
DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")


def _text(name: str) -> str:
    return (get_bundled_agents_path() / f"{name}.yaml").read_text()


def _raw(name: str) -> dict[str, Any]:
    data = yaml.safe_load(_text(name))
    assert isinstance(data, dict), name
    return data


def _load(name: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(_raw(name))


def _section(text: str, heading: str) -> str:
    """``heading`` and its body, up to the next level-2 heading."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def _copied_sections(name: str) -> dict[str, str]:
    prompt = _raw(name)["prompts"]["agent"]
    return {heading: flat(_section(prompt, heading)) for heading in COPIED_HEADINGS}


@pytest.mark.parametrize("name", NEW_SEATS)
def test_new_seat_loads_as_a_seat_agent_definition(name: str) -> None:
    raw = _raw(name)
    body = _load(name)

    assert raw["tags"] == ["gobby", "seat"]
    assert set(raw) <= set(AgentDefinitionBody.model_fields) | {"tags"}
    assert validate_workflow_definition_data(raw, expected_type="agent") == "agent"
    assert body.name == name
    assert body.version == "1.0"
    assert (body.provider, body.model, body.checkout_mode) == ("inherit", None, "inherit")
    assert body.step_workflow is None
    assert body.workflows.variables == {}


@pytest.mark.parametrize(
    ("name", "surfaces"),
    [(name, ["spawn", "persona"]) for name in STANDING_SEATS] + [("reporter", ["spawn"])],
)
def test_each_seat_declares_the_surfaces_its_role_needs(name: str, surfaces: list[str]) -> None:
    body = _load(name)

    assert body.surfaces == surfaces
    assert body.prompts.agent is not None
    assert (body.prompts.persona is not None) == ("persona" in surfaces)


@pytest.mark.parametrize("name", STANDING_SEATS)
def test_standing_seats_share_one_prompt_and_run_interactively(name: str) -> None:
    raw = _raw(name)

    assert raw["prompts"]["persona"] == raw["prompts"]["agent"]
    body = _load(name)
    assert body.execution_mode == "interactive"
    assert body.timeout == 0
    assert body.idle_ttl_seconds is None


def test_reporter_is_a_bounded_one_shot_run() -> None:
    body = _load("reporter")

    assert body.execution_mode == "one_shot"
    assert body.timeout > 0
    assert body.idle_ttl_seconds is None
    assert body.prompts.persona is None


@pytest.mark.parametrize(
    ("name", "targets"),
    [(name, ["parent", "session"]) for name in STANDING_SEATS] + [("reporter", ["session"])],
)
def test_seats_send_to_their_targets_and_never_kill_agents(name: str, targets: list[str]) -> None:
    body = _load(name)

    assert body.send_message_targets == targets
    assert body.blocked_mcp_tools == ["gobby-agents:kill_agent"]


@pytest.mark.parametrize("name", STANDING_SEATS)
def test_standing_seats_block_edit_and_write_tools_like_lane_manager(name: str) -> None:
    assert set(_load(name).blocked_tools) == WRITE_BLOCKED_TOOLS


@pytest.mark.parametrize("name", NEW_SEATS)
def test_rule_selectors_carry_the_seat_baseline_only(name: str) -> None:
    body = _load(name)
    include = set(body.workflows.rule_selectors.include)

    expected = {"tag:default", "tag:worker-safety"}
    if name != "recovery-steward":
        expected.add("name:no-send-keys")
    assert include == expected


def test_no_send_keys_resolves_for_every_new_seat_except_the_steward(
    temp_db: HubDatabase,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    rows = RuleDefinitionManager(temp_db).list_all()

    def selected(name: str) -> set[str]:
        body = _load(name)
        return {row.name for row in rows if rule_matches_agent(body, row)}

    assert "no-send-keys" not in selected("recovery-steward")
    for name in NEW_SEATS:
        if name != "recovery-steward":
            assert "no-send-keys" in selected(name), name


@pytest.mark.parametrize("name", NEW_SEATS)
def test_new_seat_prompts_open_with_a_role_and_copy_the_shared_sections(name: str) -> None:
    prompt = _raw(name)["prompts"]["agent"]

    assert prompt.lstrip().startswith("## Role")
    for heading in COPIED_HEADINGS:
        assert heading in prompt, heading
    assert _copied_sections(name) == _copied_sections("release-manager")


@pytest.mark.parametrize("name", NEW_SEATS)
def test_new_seat_files_carry_no_dates(name: str) -> None:
    assert DATE_RE.search(_text(name)) is None


def test_security_reviewer_bounces_on_exploit_paths_and_never_fixes() -> None:
    prompt = flat(_raw("security-reviewer")["prompts"]["agent"])

    for phrase in (
        "HIGH, MEDIUM or LOW",
        "exploit path",
        "LAND or BOUNCE on security grounds",
        "Never fix what you review",
    ):
        assert phrase in prompt, phrase


def test_debugger_reports_one_verdict_and_never_commits_a_fix() -> None:
    prompt = flat(_raw("debugger")["prompts"]["agent"])

    for phrase in (
        "CONFIRMED",
        "REFUTED",
        "INCONCLUSIVE",
        "Never commit a fix",
        "Never run the full test suite",
    ):
        assert phrase in prompt, phrase


def test_design_reviewer_loads_the_impeccable_contract_before_reviewing() -> None:
    prompt = flat(_raw("design-reviewer")["prompts"]["agent"])

    assert prompt.index('get_skill(name="impeccable")') < prompt.index("Accessibility.")
    for phrase in (
        ".impeccable.md",
        "WCAG 2.2 AA",
        "LAND or BOUNCE on design grounds",
        "Never fix what you review",
    ):
        assert phrase in prompt, phrase


def test_recovery_steward_resumes_idle_composers_with_literal_keystrokes() -> None:
    prompt = flat(_raw("recovery-steward")["prompts"]["agent"])

    for phrase in (
        "CronList",
        "10 minutes",
        "gobby-sessions:send_keys",
        "continue: check your queued Gobby messages and proceed\\n",
        "A bare Enter does not submit",
        "password or credential prompt",
        "Recovery is keystrokes and messages only",
    ):
        assert phrase in prompt, phrase


def test_reporter_drafts_the_status_delivers_it_and_ends_its_run() -> None:
    prompt = flat(_raw("reporter")["prompts"]["agent"])

    for phrase in (
        "gobby-agents:list_running_agents",
        'gobby-sessions:list_sessions(agent_type="assistant")',
        'gobby-agents:send_message(target="session", target_id=<session ref>)',
        'gobby-sessions:list_sessions(agent_type="orchestrator")',
        "skip rows whose status is `expired` or `archived`",
        "Decisions needed:",
        "Systems nominal",
        "Every #NNNNN carries its title",
        "gobby-agents:end_agent_run",
        "`current_state`",
        "`next_steps`",
    ):
        assert phrase in prompt, phrase
