"""End-to-end integration test for Codex MCP skill-loading gates.

Codex CLI's experimental hooks don't fire PreToolUse/PostToolUse for MCP
tool calls; SessionMessageProcessor synthesizes BEFORE_TOOL and AFTER_TOOL
HookEvents from the rollout JSONL instead. These tests verify that a
synthesized BEFORE_TOOL event for `mcp__gobby__get_tool_schema(server_name=
'gobby-tasks', tool_name='create_task')` is blocked by the bundled
`require-task-creation-skill-on-schema` rule until `loaded_skills` records the
required skill.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedToolEvent
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.observers import detect_mcp_call

pytestmark = pytest.mark.integration


# Mirrors the bundled task-creation schema gate.
_REQUIRE_TASK_CREATION_ON_SCHEMA = RuleDefinitionBody(
    event=RuleEvent.BEFORE_TOOL,
    when=(
        "(event.data.get('mcp_tool') == 'get_tool_schema'\n"
        " or event.data.get('tool_name') in ('get_tool_schema', "
        "'mcp__gobby__get_tool_schema'))\n"
        "and (tool_input.get('server_name') or tool_input.get('server')) == 'gobby-tasks'\n"
        "and (tool_input.get('tool_name') or tool_input.get('tool')) == 'create_task'\n"
        "and not skill_loaded('task-creation')\n"
    ),
    effects=[
        RuleEffect(
            type="block",
            reason=skill_fetch_directive("task-creation"),
        ),
    ],
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
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
        priority=23,
        enabled=True,
        sources=None,
    )


@pytest.mark.asyncio
async def test_synthesized_before_tool_blocks_for_task_creation_skill(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    _install_rule(
        manager,
        "require-task-creation-skill-on-schema",
        _REQUIRE_TASK_CREATION_ON_SCHEMA,
    )

    tool_event = ParsedToolEvent(
        phase="begin",
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

    assert response.decision == "block"
    assert 'Call get_skill(name="task-creation") on gobby-skills, then continue.' in (
        response.reason or ""
    )
    assert "loaded_skills" not in variables


@pytest.mark.asyncio
async def test_get_skill_after_tool_updates_loaded_skills_and_suppresses_next_prompt(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    _install_rule(
        manager,
        "require-task-creation-skill-on-schema",
        _REQUIRE_TASK_CREATION_ON_SCHEMA,
    )

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
        phase="begin",
        call_id="call_2",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-tasks", "tool_name": "create_task"},
        timestamp=datetime.now(UTC),
        raw_json={},
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
    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_synthesized_event_skipped_when_skill_already_loaded(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """The rule's canonical loaded_skills idempotency guard applies."""
    _install_rule(
        manager,
        "require-task-creation-skill-on-schema",
        _REQUIRE_TASK_CREATION_ON_SCHEMA,
    )

    tool_event = ParsedToolEvent(
        phase="begin",
        call_id="call_2",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-tasks", "tool_name": "create_task"},
        timestamp=datetime.now(UTC),
        raw_json={},
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

    assert response.decision == "allow"
    assert variables["loaded_skills"] == ["task-creation"]


@pytest.mark.asyncio
async def test_synthesized_event_not_skipped_when_skill_legacy_injected(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """Legacy injected_skills no longer satisfies skill_loaded()."""
    _install_rule(
        manager,
        "require-task-creation-skill-on-schema",
        _REQUIRE_TASK_CREATION_ON_SCHEMA,
    )

    tool_event = ParsedToolEvent(
        phase="begin",
        call_id="call_legacy",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-tasks", "tool_name": "create_task"},
        timestamp=datetime.now(UTC),
        raw_json={},
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

    assert response.decision == "block"
    assert 'Call get_skill(name="task-creation") on gobby-skills, then continue.' in (
        response.reason or ""
    )


def test_synthesized_event_requires_non_empty_external_id(
    caplog: pytest.LogCaptureFixture,
    enable_log_propagation: None,
) -> None:
    tool_event = ParsedToolEvent(
        phase="begin",
        call_id="call_missing_external",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-tasks", "tool_name": "create_task"},
        timestamp=datetime.now(UTC),
        raw_json={},
    )
    caplog.set_level("WARNING", logger="gobby.sessions.processor")

    hook_event = SessionMessageProcessor._build_codex_hook_event(
        {
            "external_id": "",
            "machine_id": "machine-xyz",
            "project_id": "project-abc",
            "platform_session_id": "platform-sid",
        },
        tool_event,
    )

    assert hook_event is None
    assert "without external_id" in caplog.text


@pytest.mark.asyncio
async def test_synthesized_event_for_unrelated_server_does_not_fire(
    db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    _install_rule(
        manager,
        "require-task-creation-skill-on-schema",
        _REQUIRE_TASK_CREATION_ON_SCHEMA,
    )

    tool_event = ParsedToolEvent(
        phase="begin",
        call_id="call_3",
        server="gobby",
        tool="get_tool_schema",
        arguments={"server_name": "gobby-memory", "tool_name": "create_memory"},
        timestamp=datetime.now(UTC),
        raw_json={},
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
    assert "loaded_skills" not in variables
