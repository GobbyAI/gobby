"""Rule disable propagation regressions."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "rule_disable_propagation.db")
    run_migrations(database)
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
async def test_disable_rule_takes_effect_on_next_event_in_process(db: LocalDatabase) -> None:
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
async def test_disable_rule_takes_effect_across_processes(db: LocalDatabase) -> None:
    manager = LocalWorkflowDefinitionManager(db)
    rule_id = _make_blocking_rule(manager)
    engine = RuleEngine(db)
    event = _make_bash_event()

    first = await engine.evaluate(event, session_id="session-rule-disable", variables={})
    _disable_rule_in_child_process(db.db_path, rule_id)
    second = await engine.evaluate(event, session_id="session-rule-disable", variables={})

    assert first.decision == "block"
    assert second.decision == "allow"


def _disable_rule_in_child_process(db_path: Path, rule_id: str) -> None:
    script = """
from pathlib import Path
import sys

from gobby.storage.database import LocalDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

manager = LocalWorkflowDefinitionManager(LocalDatabase(Path(sys.argv[1])))
manager.update(sys.argv[2], enabled=False)
"""
    subprocess.run([sys.executable, "-c", script, str(db_path), rule_id], check=True)
