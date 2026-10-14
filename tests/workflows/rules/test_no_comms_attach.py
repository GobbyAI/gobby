"""Whole-file delivery stays distinct from the exempt seats' redacting log CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.command_matching import command_patterns_match
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.selectors import resolve_rules_for_agent
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "no-comms-attach"
RULE_PATH = get_bundled_rules_path() / "tool-hygiene" / f"{RULE_NAME}.yaml"
EXEMPT_SEATS = {"reporter", "log-monitor", "metrics-monitor", "trajectory-monitor"}
AGENT_PATHS = sorted(get_bundled_agents_path().glob("*.yaml"))
SESSION_ID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def rule_row() -> RuleDefinitionRow:
    data = yaml.safe_load(RULE_PATH.read_text())
    definition = dict(data["rules"][RULE_NAME])
    enabled = definition.pop("enabled")
    priority = definition.pop("priority")
    description = definition.pop("description")
    RuleDefinitionBody.model_validate(definition)
    now = datetime.now(UTC)
    return RuleDefinitionRow(
        id=SESSION_ID,
        name=RULE_NAME,
        enabled=enabled,
        enabled_pinned=False,
        priority=priority,
        description=description,
        definition_json=definition,
        source="installed",
        created_at=now,
        updated_at=now,
        tags=data["tags"],
    )


@pytest.mark.parametrize(
    "command",
    [
        "gobby comms attach channel excerpt.txt",
        "gobby comms attach --caption 'redacted lines' channel excerpt.txt",
        "gobby comms attach channel excerpt.txt --caption 'redacted lines'",
        "uv run gobby comms attach channel excerpt.log",
        "uv run --no-sync gobby comms attach channel excerpt.txt",
        "uv --directory '/workspace path' run --no-sync gobby comms attach channel excerpt.txt",
        "gobby --config config.yaml comms attach channel excerpt.txt",
        "gobby --config='config path.yaml' comms attach channel excerpt.txt",
        "uv run gobby --config 'config path.yaml' comms attach channel excerpt.txt",
        "FLAG=1 command /bin/gobby comms attach channel excerpt.txt",
        "env FLAG=1 /bin/gobby comms attach channel excerpt.txt",
        "cat excerpt.txt | gobby comms attach channel excerpt.txt",
        "echo ready && gobby comms attach channel excerpt.txt",
        "echo ready; gobby comms attach channel excerpt.txt",
        "echo $(gobby comms attach channel excerpt.txt)",
        "bash -c 'gobby comms attach channel excerpt.txt'",
        "'gobby' 'comms' 'attach' channel excerpt.txt",
        r"gob\by co\mms att\ach channel excerpt.txt",
    ],
)
def test_blocks_attachment_invocations(rule_row: RuleDefinitionRow, command: str) -> None:
    effect = RuleDefinitionBody.model_validate(rule_row.definition_json).resolved_effects[0]
    assert command_patterns_match(command, pattern=effect.command_pattern)


@pytest.mark.parametrize(
    "command",
    [
        "gobby comms status",
        "uv run --no-sync gobby comms channels list",
        "gobby comms send channel 'gobby comms attach'",
        "echo 'gobby comms attach channel excerpt.txt'",
        "printf '%s' 'gobby comms attach'",
        "cat <<'EOF'\ngobby comms attach channel excerpt.txt\nEOF",
        "gobby comms attachment channel excerpt.txt",
        "other-gobby comms attach channel excerpt.txt",
        "gobby --config 'comms attach' comms status",
    ],
)
def test_allows_other_commands_and_literal_data(rule_row: RuleDefinitionRow, command: str) -> None:
    effect = RuleDefinitionBody.model_validate(rule_row.definition_json).resolved_effects[0]
    assert not command_patterns_match(command, pattern=effect.command_pattern)


@pytest.mark.parametrize("path", AGENT_PATHS, ids=lambda path: path.stem)
def test_only_reporter_and_monitors_exclude_the_rule(
    rule_row: RuleDefinitionRow, path: Path
) -> None:
    agent = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))
    selected = resolve_rules_for_agent(agent, [rule_row])
    assert (RULE_NAME in selected) is (agent.name not in EXEMPT_SEATS)
    assert (f"name:{RULE_NAME}" in agent.workflows.rule_selectors.exclude) is (
        agent.name in EXEMPT_SEATS
    )


async def test_engine_blocks_default_seat_and_allows_unrelated_comms(temp_db: HubDatabase) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("DELETE FROM rule_definitions WHERE name != %s", (RULE_NAME,))
    engine = RuleEngine(temp_db)

    async def decide(command: str) -> tuple[str, str]:
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Bash", "tool_input": {"command": command}},
        )
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"_agent_type": "default"}
        )
        return response.decision, response.reason or ""

    blocked = await decide("uv run --no-sync gobby comms attach channel excerpt.txt")
    allowed = await decide("gobby comms status")
    assert blocked[0] == "block"
    assert "gobby-communications:send_operator_attachment" in blocked[1]
    assert "Log Monitor" in blocked[1]
    assert allowed[0] == "allow"


def test_assistant_routes_whole_files_and_cited_logs() -> None:
    data = yaml.safe_load((get_bundled_agents_path() / "assistant.yaml").read_text())
    prompt: str = data["prompts"]["agent"]
    assert "gobby comms attach" not in prompt
    assert "gobby-communications:send_operator_attachment" in prompt
    assert "files and plans" in prompt
    assert "Log Monitor through `gobby-agents:send_message`" in prompt
    assert "ask the Log Monitor to deliver cited log lines redacted" in prompt
    assert "never the log lines or a path" in prompt


def test_attachment_reference_explains_seat_exemptions() -> None:
    reference = (
        get_bundled_agents_path().parents[1]
        / "skills/gobby/references/communications/attachments.md"
    ).read_text()
    for seat in ("Reporter", "Log Monitor", "Metrics Monitor", "Trajectory Monitor"):
        assert seat in reference
    assert "`no-comms-attach`" in reference
    assert "without redaction" in reference
    assert "Every other seat uses `send_operator_attachment`" in reference
