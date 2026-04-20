"""End-to-end integration test for Codex MCP skill injection.

Codex CLI's experimental hooks don't fire PreToolUse/PostToolUse for MCP
tool calls; SessionMessageProcessor synthesizes BEFORE_TOOL/AFTER_TOOL
HookEvents from the rollout JSONL instead. This test verifies that a
synthesized AFTER_TOOL event for `mcp__gobby__get_tool_schema(server_name=
'gobby-tasks', tool_name='create_task')` causes the bundled
`inject-task-creation-on-schema` rule to fire end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedToolEvent
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.rule_engine import RuleEngine

pytestmark = pytest.mark.integration


# Mirrors the bundled YAML at
# src/gobby/install/shared/workflows/rules/task-enforcement/inject-task-creation-on-schema.yaml.
_INJECT_TASK_CREATION_ON_SCHEMA = RuleDefinitionBody(
    event=RuleEvent.AFTER_TOOL,
    when=(
        "(event.data.get('mcp_tool') == 'get_tool_schema'\n"
        " or event.data.get('tool_name') in ('get_tool_schema', "
        "'mcp__gobby__get_tool_schema'))\n"
        "and tool_input.get('server_name') == 'gobby-tasks'\n"
        "and tool_input.get('tool_name') in ('create_task', 'claim_task')\n"
        "and 'task-creation' not in variables.get('injected_skills', [])\n"
    ),
    effects=[
        # The bundled rule has a sibling mcp_call(get_skill, inject_result=true)
        # before the set_variable. With no MCP dispatcher wired, the engine
        # takes the deferred-dispatch branch (rule_engine effects.py:141) and
        # the subsequent set_variable still runs — which is exactly the bit
        # that drives the injected_skills ledger.
        RuleEffect(
            type="mcp_call",
            server="gobby-skills",
            tool="get_skill",
            arguments={"name": "task-creation"},
            inject_result=True,
        ),
        RuleEffect(
            type="set_variable",
            variable="injected_skills",
            value="variables.get('injected_skills', []) + ['task-creation']",
        ),
    ],
)


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "codex_skill_injection.db")
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _install_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
) -> None:
    manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=25,
        enabled=True,
        sources=None,
    )


@pytest.mark.asyncio
async def test_synthesized_after_tool_fires_inject_task_creation_rule(
    db: LocalDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    _install_rule(manager, "inject-task-creation-on-schema", _INJECT_TASK_CREATION_ON_SCHEMA)

    tool_event = ParsedToolEvent(
        phase="end",
        call_id="call_hJqEH1DUthWw8MWcBZK1E2iQ",  # real call_id from the #2995 rollout
        server="gobby",
        tool="get_tool_schema",
        arguments={
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
            "session_id": "#2995",
        },
        timestamp=datetime(2026, 4, 20, 4, 5, 7, 591000, tzinfo=UTC),
        raw_json={},
        result={"content": [{"type": "text", "text": "{...}"}]},
        duration_ns=18_695_333,
    )

    hook_event = SessionMessageProcessor._build_codex_hook_event("sid", tool_event)
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {}

    response = await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert response.decision == "allow"
    assert variables.get("injected_skills") == ["task-creation"]


@pytest.mark.asyncio
async def test_synthesized_event_skipped_when_skill_already_injected(
    db: LocalDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """The rule's idempotency guard ('not in injected_skills') still applies."""
    _install_rule(manager, "inject-task-creation-on-schema", _INJECT_TASK_CREATION_ON_SCHEMA)

    tool_event = ParsedToolEvent(
        phase="end",
        call_id="call_2",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-tasks", "tool_name": "claim_task"},
        timestamp=datetime.now(UTC),
        raw_json={},
        result={"ok": True},
    )
    hook_event = SessionMessageProcessor._build_codex_hook_event("sid", tool_event)
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {"injected_skills": ["task-creation"]}
    await engine.evaluate(hook_event, session_id="sid", variables=variables)

    # No change; rule no-ops because task-creation is already present.
    assert variables["injected_skills"] == ["task-creation"]


@pytest.mark.asyncio
async def test_synthesized_event_for_unrelated_server_does_not_fire(
    db: LocalDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    _install_rule(manager, "inject-task-creation-on-schema", _INJECT_TASK_CREATION_ON_SCHEMA)

    tool_event = ParsedToolEvent(
        phase="end",
        call_id="call_3",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-memory", "tool_name": "create_memory"},
        timestamp=datetime.now(UTC),
        raw_json={},
        result={"ok": True},
    )
    hook_event = SessionMessageProcessor._build_codex_hook_event("sid", tool_event)
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {}
    await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert "injected_skills" not in variables or variables["injected_skills"] == []
