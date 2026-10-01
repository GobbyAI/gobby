"""Rule disable propagation regressions."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions import notifications
from gobby.storage.definitions.notifications import DefinitionRevisionListener
from gobby.storage.definitions.revisions import (
    DefinitionDomain,
    bump_definitions_revision,
    fetch_persistent_revisions,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.integration


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _make_blocking_rule(manager: RuleDefinitionManager) -> str:
    body = RuleDefinitionBody(
        event="before_tool",
        when="event.data.get('tool_name') == 'Bash'",
        effects=[RuleEffect(type="block", reason="disabled propagation probe")],
        group="test",
    )
    row = manager.create(
        name="disable-propagation-probe",
        definition_json=body.model_dump_json(),
        enabled=True,
        priority=1,
        source="installed",
        tags=["gobby"],
    )
    return row.id


def _make_bash_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="11111111-1111-4111-8111-111111111111",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    )


@pytest.mark.asyncio
async def test_disable_rule_takes_effect_on_next_event_in_process(db: HubDatabase) -> None:
    manager = RuleDefinitionManager(db)
    rule_id = _make_blocking_rule(manager)
    engine = RuleEngine(db)
    event = _make_bash_event()

    first = await engine.evaluate(
        event, session_id="11111111-1111-4111-8111-111111111111", variables={}
    )
    manager.update(rule_id, enabled=False)
    second = await engine.evaluate(
        event, session_id="11111111-1111-4111-8111-111111111111", variables={}
    )

    assert first.decision == "block"
    assert second.decision == "allow"


@pytest.mark.asyncio
async def test_disable_rule_takes_effect_across_processes(
    db: HubDatabase,
    postgres_database_url: str,
    postgres_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RuleDefinitionManager(db)
    rule_id = _make_blocking_rule(manager)
    engine = RuleEngine(db)
    event = _make_bash_event()

    first = await engine.evaluate(
        event, session_id="11111111-1111-4111-8111-111111111111", variables={}
    )
    revision_received = asyncio.Event()

    def observe_revision(*domains: DefinitionDomain) -> None:
        bump_definitions_revision(*domains)
        if "rules" in domains:
            revision_received.set()

    monkeypatch.setattr(notifications, "bump_definitions_revision", observe_revision)

    async def open_listener() -> psycopg.AsyncConnection[Any]:
        return await psycopg.AsyncConnection.connect(
            make_conninfo(postgres_database_url, options=f"-c search_path={postgres_schema}"),
            autocommit=True,
        )

    listener = DefinitionRevisionListener(
        open_listener,
        fetch_revisions=lambda: fetch_persistent_revisions(db),
    )
    await listener.start()
    try:
        await asyncio.to_thread(
            _disable_rule_in_child_process, postgres_database_url, postgres_schema, rule_id
        )
        await asyncio.wait_for(revision_received.wait(), timeout=5.0)
        second = await engine.evaluate(
            event, session_id="11111111-1111-4111-8111-111111111111", variables={}
        )
    finally:
        await listener.close()

    assert first.decision == "block"
    assert second.decision == "allow"


def _disable_rule_in_child_process(database_url: str, schema: str, rule_id: str) -> None:
    script = """
import sys

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager

database_url, schema, rule_id = sys.argv[1:]
db = PostgresHubDatabase(database_url + f"?options=-csearch_path%3D{schema}")
try:
    manager = RuleDefinitionManager(db)
    manager.update(rule_id, enabled=False)
finally:
    db.close()
"""
    subprocess.run([sys.executable, "-c", script, database_url, schema, rule_id], check=True)
