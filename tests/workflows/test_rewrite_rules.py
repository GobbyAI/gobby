"""Tests for rewrite_input rules: MCP nesting, skip_validation strip, require-uv, regex_replace."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.rule_engine import RuleEngine
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_rewrite.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _insert_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
    enabled: bool = True,
) -> str:
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=priority,
        enabled=enabled,
    )
    return row.id


class TestMCPRewriteNesting:
    """rewrite_input should auto-nest updates inside `arguments` for MCP call_tool."""

    @pytest.mark.asyncio
    async def test_rewrite_nests_inside_arguments(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "strip-flag",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(
                    type="rewrite_input",
                    input_updates={"skip_validation": False},
                    auto_approve=True,
                ),
            ),
        )

        event = _make_event(
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "t-1", "skip_validation": True},
                },
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        assert response.modified_input is not None
        # Updates should be nested inside arguments, not at top level
        assert "arguments" in response.modified_input
        inner = response.modified_input["arguments"]
        assert inner["skip_validation"] is False
        # Original arguments should be preserved
        assert inner["task_id"] == "t-1"

    @pytest.mark.asyncio
    async def test_rewrite_native_tool_stays_flat(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """For native tools (not call_tool), updates should remain top-level."""
        _insert_rule(
            manager,
            "rewrite-command",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(
                    type="rewrite_input",
                    input_updates={"command": "uv run python script.py"},
                    auto_approve=True,
                ),
            ),
        )

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        assert response.modified_input is not None
        assert response.modified_input["command"] == "uv run python script.py"
        assert "arguments" not in response.modified_input

    @pytest.mark.asyncio
    async def test_rewrite_mcp_string_arguments(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When arguments is a JSON string, it should be parsed before merging."""
        _insert_rule(
            manager,
            "strip-flag",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(
                    type="rewrite_input",
                    input_updates={"skip_validation": False},
                    auto_approve=True,
                ),
            ),
        )

        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": json.dumps({"task_id": "t-2", "skip_validation": True}),
                },
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.modified_input is not None
        inner = response.modified_input["arguments"]
        assert inner["skip_validation"] is False
        assert inner["task_id"] == "t-2"


class TestStripSkipValidation:
    """Tests for the strip-skip-validation-with-commit rule pattern."""

    @pytest.mark.asyncio
    async def test_strips_skip_validation_with_commits(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "strip-skip-validation-with-commit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when=(
                    "tool_input.get('skip_validation') "
                    "and (variables.get('task_has_commits') or tool_input.get('commit_sha')) "
                    "and tool_input.get('tool_name') == 'close_task'"
                ),
                effects=[
                    RuleEffect(
                        type="inject_context",
                        template="Gobby stripped skip_validation from your close_task call.",
                    ),
                    RuleEffect(
                        type="rewrite_input",
                        input_updates={"skip_validation": False},
                        auto_approve=True,
                    ),
                ],
            ),
        )

        event = _make_event(
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "t-1", "skip_validation": True},
                },
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"task_has_commits": True}
        )

        assert response.decision == "allow"
        # Should have inject_context
        assert "stripped skip_validation" in (response.context or "")
        # Should rewrite
        assert response.modified_input is not None
        inner = response.modified_input["arguments"]
        assert inner["skip_validation"] is False

    @pytest.mark.asyncio
    async def test_passthrough_without_commits(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rule should NOT fire when no commits are attached."""
        _insert_rule(
            manager,
            "strip-skip-validation-with-commit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when=(
                    "tool_input.get('skip_validation') "
                    "and (variables.get('task_has_commits') or tool_input.get('commit_sha')) "
                    "and tool_input.get('tool_name') == 'close_task'"
                ),
                effects=[
                    RuleEffect(
                        type="inject_context",
                        template="Gobby stripped skip_validation.",
                    ),
                    RuleEffect(
                        type="rewrite_input",
                        input_updates={"skip_validation": False},
                        auto_approve=True,
                    ),
                ],
            ),
        )

        event = _make_event(
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "t-1", "skip_validation": True},
                },
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"task_has_commits": False}
        )

        assert response.decision == "allow"
        assert response.modified_input is None


class TestRegexReplaceFilter:
    """Tests for the regex_replace Jinja2 filter."""

    def test_simple_replace(self) -> None:
        engine = TemplateEngine()
        result = engine.render(
            "{{ text | regex_replace('pip', 'uv pip') }}",
            {"text": "pip install foo"},
        )
        assert result == "uv pip install foo"

    def test_no_match_passthrough(self) -> None:
        engine = TemplateEngine()
        result = engine.render(
            "{{ text | regex_replace('pip', 'uv pip') }}",
            {"text": "echo hello"},
        )
        assert result == "echo hello"

    def test_pattern_groups(self) -> None:
        engine = TemplateEngine()
        result = engine.render(
            r"{{ text | regex_replace('(^|(?<=[;&|]))(\\s*)pip', '\\1\\2uv pip') }}",
            {"text": "pip install foo"},
        )
        assert "uv pip" in result


class TestShlexQuoteFilter:
    """Tests for the shlex_quote Jinja2 filter."""

    def test_simple_quote(self) -> None:
        engine = TemplateEngine()
        result = engine.render(
            "gobby compress -- {{ cmd | shlex_quote }}",
            {"cmd": "git log --oneline"},
        )
        assert result == "gobby compress -- 'git log --oneline'"

    def test_metacharacters_escaped(self) -> None:
        engine = TemplateEngine()
        result = engine.render(
            "gobby compress -- {{ cmd | shlex_quote }}",
            {"cmd": "echo hello; rm -rf /"},
        )
        # shlex.quote wraps in single quotes, neutralizing the semicolon
        assert result == "gobby compress -- 'echo hello; rm -rf /'"

    def test_empty_string(self) -> None:
        engine = TemplateEngine()
        result = engine.render(
            "{{ cmd | shlex_quote }}",
            {"cmd": ""},
        )
        assert result == "''"


class TestRequireUvRewrite:
    """Tests for the require-uv rewrite rule pattern."""

    @pytest.mark.asyncio
    async def test_rewrites_bare_python(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "require-uv",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="variables.get('require_uv') and event.data.get('tool_name') == 'Bash'",
                effects=[
                    RuleEffect(
                        type="inject_context",
                        when="any(p in str(tool_input.get('command', '')) for p in ['python', 'pip'])",
                        template="Gobby rewrote your command to use uv.",
                    ),
                    RuleEffect(
                        type="rewrite_input",
                        when="any(p in str(tool_input.get('command', '')) for p in ['python', 'pip'])",
                        input_updates={
                            "command": (
                                "{{ event.data.tool_input.command"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?pip3?\\\\b', '\\\\1\\\\2uv pip')"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?python(?:3(?:\\\\.\\\\d+)?)?\\\\b', '\\\\1\\\\2uv run python') }}"
                            ),
                        },
                        auto_approve=True,
                    ),
                ],
            ),
        )

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.modified_input is not None
        assert "uv run python" in response.modified_input["command"]
        assert response.auto_approve is True

    @pytest.mark.asyncio
    async def test_passthrough_uv_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Commands already using uv should not be modified (regex won't match)."""
        _insert_rule(
            manager,
            "require-uv",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="variables.get('require_uv') and event.data.get('tool_name') == 'Bash'",
                effects=[
                    RuleEffect(
                        type="rewrite_input",
                        when="any(p in str(tool_input.get('command', '')) for p in ['python', 'pip'])",
                        input_updates={
                            "command": (
                                "{{ event.data.tool_input.command"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?pip3?\\\\b', '\\\\1\\\\2uv pip')"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?python(?:3(?:\\\\.\\\\d+)?)?\\\\b', '\\\\1\\\\2uv run python') }}"
                            ),
                        },
                        auto_approve=True,
                    ),
                ],
            ),
        )

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "uv run python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"require_uv": True}
        )

        assert response.decision == "allow"
        # The per-effect when still matches ('python' is in the string), so rewrite fires,
        # but the regex shouldn't match 'uv run python' because it's preceded by 'uv run '
        # not by a statement separator. The result should still contain 'uv run python'.
        assert response.modified_input is not None
        assert "uv run python" in response.modified_input["command"]

    @pytest.mark.asyncio
    async def test_compound_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Compound commands should only rewrite the python/pip parts."""
        _insert_rule(
            manager,
            "require-uv",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="variables.get('require_uv') and event.data.get('tool_name') == 'Bash'",
                effects=[
                    RuleEffect(
                        type="rewrite_input",
                        when="any(p in str(tool_input.get('command', '')) for p in ['python', 'pip'])",
                        input_updates={
                            "command": (
                                "{{ event.data.tool_input.command"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?pip3?\\\\b', '\\\\1\\\\2uv pip')"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?python(?:3(?:\\\\.\\\\d+)?)?\\\\b', '\\\\1\\\\2uv run python') }}"
                            ),
                        },
                        auto_approve=True,
                    ),
                ],
            ),
        )

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi && pip install foo"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.modified_input is not None
        cmd = response.modified_input["command"]
        assert "echo hi" in cmd
        assert "uv pip" in cmd

    @pytest.mark.asyncio
    async def test_non_python_command_no_rewrite(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-python Bash commands should not be rewritten."""
        _insert_rule(
            manager,
            "require-uv",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="variables.get('require_uv') and event.data.get('tool_name') == 'Bash'",
                effects=[
                    RuleEffect(
                        type="rewrite_input",
                        when="any(p in str(tool_input.get('command', '')) for p in ['python', 'pip'])",
                        input_updates={
                            "command": (
                                "{{ event.data.tool_input.command"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?pip3?\\\\b', '\\\\1\\\\2uv pip')"
                                " | regex_replace('(^|(?<=[;&|]))(\\\\s*)(?:sudo\\\\s+)?python(?:3(?:\\\\.\\\\d+)?)?\\\\b', '\\\\1\\\\2uv run python') }}"
                            ),
                        },
                        auto_approve=True,
                    ),
                ],
            ),
        )

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"require_uv": True}
        )

        assert response.decision == "allow"
        # Per-effect when blocks the rewrite since 'python'/'pip' not in command
        assert response.modified_input is None
