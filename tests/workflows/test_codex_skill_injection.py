"""End-to-end integration test for Codex MCP skill loading directives.

Codex CLI's experimental hooks don't fire PreToolUse/PostToolUse for MCP
tool calls; SessionMessageProcessor synthesizes BEFORE_TOOL/AFTER_TOOL
HookEvents from the rollout JSONL instead. This test verifies that a
synthesized AFTER_TOOL event for `mcp__gobby__get_tool_schema(server_name=
'gobby-tasks', tool_name='create_task')` causes the bundled
`inject-task-creation-on-schema` rule to emit an on-demand skill directive.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedToolEvent
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.observers import detect_mcp_call

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
        "and not skill_loaded('task-creation')\n"
    ),
    effects=[
        RuleEffect(
            type="inject_context",
            template='Call get_skill(name="task-creation") on gobby-skills, then continue.',
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
async def test_synthesized_after_tool_emits_task_creation_directive(
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

    hook_event = SessionMessageProcessor._build_codex_hook_event(
        {
            "external_id": "external-sid",
            "machine_id": "machine-xyz",
            "project_id": "project-abc",
            "platform_session_id": "platform-sid",
        },
        tool_event,
    )
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {}

    response = await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert response.decision == "allow"
    assert (
        response.context == 'Call get_skill(name="task-creation") on gobby-skills, then continue.'
    )
    assert "loaded_skills" not in variables


@pytest.mark.asyncio
async def test_get_skill_after_tool_updates_loaded_skills_and_suppresses_next_prompt(
    db: LocalDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    _install_rule(manager, "inject-task-creation-on-schema", _INJECT_TASK_CREATION_ON_SCHEMA)

    variables: dict[str, object] = {}
    detect_mcp_call(
        HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            source=SessionSource.CODEX,
            session_id="external-sid",
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {"server_name": "gobby-skills", "tool_name": "get_skill"},
                "tool_output": {"result": {"success": True, "skill": {"name": "task-creation"}}},
                "mcp_server": "gobby-skills",
                "mcp_tool": "get_skill",
            },
            metadata={"_platform_session_id": "sid"},
        ),
        variables,
        "sid",
    )

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
    hook_event = SessionMessageProcessor._build_codex_hook_event(
        {
            "external_id": "external-sid",
            "machine_id": "machine-xyz",
            "project_id": "project-abc",
            "platform_session_id": "platform-sid",
        },
        tool_event,
    )
    assert hook_event is not None

    engine = RuleEngine(db)
    response = await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert variables["loaded_skills"] == ["task-creation"]
    assert response.context is None


@pytest.mark.asyncio
async def test_synthesized_event_skipped_when_skill_already_loaded(
    db: LocalDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """The rule's canonical loaded_skills idempotency guard applies."""
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
    hook_event = SessionMessageProcessor._build_codex_hook_event(
        {
            "external_id": "external-sid",
            "machine_id": "machine-xyz",
            "project_id": "project-abc",
            "platform_session_id": "platform-sid",
        },
        tool_event,
    )
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {"loaded_skills": ["task-creation"]}
    response = await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert response.context is None
    assert variables["loaded_skills"] == ["task-creation"]


@pytest.mark.asyncio
async def test_synthesized_event_not_skipped_when_skill_legacy_injected(
    db: LocalDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """Legacy injected_skills no longer satisfies skill_loaded()."""
    _install_rule(manager, "inject-task-creation-on-schema", _INJECT_TASK_CREATION_ON_SCHEMA)

    tool_event = ParsedToolEvent(
        phase="end",
        call_id="call_legacy",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-tasks", "tool_name": "claim_task"},
        timestamp=datetime.now(UTC),
        raw_json={},
        result={"ok": True},
    )
    hook_event = SessionMessageProcessor._build_codex_hook_event(
        {
            "external_id": "external-sid",
            "machine_id": "machine-xyz",
            "project_id": "project-abc",
            "platform_session_id": "platform-sid",
        },
        tool_event,
    )
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {"injected_skills": ["task-creation"]}
    response = await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert (
        response.context == 'Call get_skill(name="task-creation") on gobby-skills, then continue.'
    )


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
    hook_event = SessionMessageProcessor._build_codex_hook_event(
        {
            "external_id": "external-sid",
            "machine_id": "machine-xyz",
            "project_id": "project-abc",
            "platform_session_id": "platform-sid",
        },
        tool_event,
    )
    assert hook_event is not None

    engine = RuleEngine(db)
    variables: dict[str, object] = {}
    response = await engine.evaluate(hook_event, session_id="sid", variables=variables)

    assert response.context is None
    assert "loaded_skills" not in variables
