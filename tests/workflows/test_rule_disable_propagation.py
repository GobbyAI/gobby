"""Rule disable propagation regressions."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.integration


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _make_blocking_rule(manager: LocalWorkflowDefinitionManager) -> str:
    body = RuleDefinitionBody(
        event="before_tool",
        when="event.data.get('tool_name') == 'Bash'",
        effects=[RuleEffect(type="block", reason="disabled propagation probe")],
        group="test",
    )
    row = manager.create(
        name="disable-propagation-probe",
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        enabled=True,
        priority=1,
        source="installed",
        tags=["gobby"],
    )
    return row.id


def _make_bash_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="session-rule-disable",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    )


@pytest.mark.asyncio
async def test_disable_rule_takes_effect_on_next_event_in_process(db: HubDatabase) -> None:
    manager = LocalWorkflowDefinitionManager(db)
    rule_id = _make_blocking_rule(manager)
    engine = RuleEngine(db)
    event = _make_bash_event()

    first = await engine.evaluate(event, session_id="session-rule-disable", variables={})
    manager.update(rule_id, enabled=False)
    second = await engine.evaluate(event, session_id="session-rule-disable", variables={})

    assert first.decision == "block"
    assert second.decision == "allow"


@pytest.mark.asyncio
async def test_disable_rule_takes_effect_across_processes(
    db: HubDatabase,
    postgres_database_url: str,
    postgres_schema: str,
) -> None:
    manager = LocalWorkflowDefinitionManager(db)
    rule_id = _make_blocking_rule(manager)
    engine = RuleEngine(db)
    event = _make_bash_event()

    first = await engine.evaluate(event, session_id="session-rule-disable", variables={})
    _disable_rule_in_child_process(postgres_database_url, postgres_schema, rule_id)
    second = await engine.evaluate(event, session_id="session-rule-disable", variables={})

    assert first.decision == "block"
    assert second.decision == "allow"


def _disable_rule_in_child_process(database_url: str, schema: str, rule_id: str) -> None:
    script = """
import sys

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

database_url, schema, rule_id = sys.argv[1:]
db = PostgresHubDatabase(database_url + f"?options=-csearch_path%3D{schema}")
try:
    manager = LocalWorkflowDefinitionManager(db)
    manager.update(rule_id, enabled=False)
finally:
    db.close()
"""
    subprocess.run([sys.executable, "-c", script, database_url, schema, rule_id], check=True)
