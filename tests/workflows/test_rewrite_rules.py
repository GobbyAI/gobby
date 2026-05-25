"""Tests for rewrite_input rules and require-uv block regression coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
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


def _sync_bundled_rules(db: HubDatabase) -> None:
    """Sync the real bundled rule set into the test database."""
    sync_bundled_rules(db, get_bundled_rules_path())


def _load_bundled_rule(
    manager: LocalWorkflowDefinitionManager,
    rule_name: str,
) -> str:
    """Load one bundled rule by name, exercising the production YAML `when:` clause.

    Walks the bundled rules tree, finds the rule, and inserts it directly so the
    test isolates the rule under test from sibling rules' preconditions.
    """
    import yaml

    rules_path = get_bundled_rules_path()
    for yaml_file in sorted(rules_path.rglob("*.yaml")):
        if "deprecated" in yaml_file.relative_to(rules_path).parts:
            continue
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        rules = data.get("rules") or {}
        if rule_name not in rules:
            continue
        body = RuleDefinitionBody.model_validate(rules[rule_name])
        priority = rules[rule_name].get("priority", 100)
        enabled = rules[rule_name].get("enabled", True)
        return _insert_rule(manager, rule_name, body, priority=priority, enabled=enabled)
    raise AssertionError(f"Bundled rule {rule_name!r} not found under {rules_path}")


class TestMCPRewriteNesting:
    """rewrite_input should auto-nest updates inside `arguments` for MCP call_tool."""

    @pytest.mark.asyncio
    async def test_rewrite_nests_inside_arguments(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "strip-flag",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="rewrite_input",
                        input_updates={"skip_validation": False},
                        auto_approve=True,
                    )
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
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """For native tools (not call_tool), updates should remain top-level."""
        _insert_rule(
            manager,
            "rewrite-command",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="rewrite_input",
                        input_updates={"command": "uv run python script.py"},
                        auto_approve=True,
                    )
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
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        assert response.modified_input is not None
        assert response.modified_input["command"] == "uv run python script.py"
        assert "arguments" not in response.modified_input

    @pytest.mark.asyncio
    async def test_rewrite_mcp_string_arguments(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When arguments is a JSON string, it should be parsed before merging."""
        _insert_rule(
            manager,
            "strip-flag",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="rewrite_input",
                        input_updates={"skip_validation": False},
                        auto_approve=True,
                    )
                ],
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
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _load_bundled_rule(manager, "strip-skip-validation-with-commit")

        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
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
        assert "stripped skip_validation" in (response.context or "")
        assert response.modified_input is not None
        inner = response.modified_input["arguments"]
        assert inner["skip_validation"] is False

    @pytest.mark.asyncio
    async def test_passthrough_without_commits(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rule should NOT fire when no commits are attached."""
        _load_bundled_rule(manager, "strip-skip-validation-with-commit")

        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
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

    @pytest.mark.asyncio
    async def test_does_not_fire_for_other_servers(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rule must scope to gobby-tasks::close_task, not any close_task lookalike."""
        _load_bundled_rule(manager, "strip-skip-validation-with-commit")

        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "some-other-server",
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


REQUIRE_UV_COMMAND_PATTERN = r"(^|(?<=[;&|]))\s*(?:sudo\s+)?(?:pip3?\b|python(?:3(?:\.\d+)?)?\b)"
REQUIRE_UV_REASON = "Bare python/pip is not permitted in this repo. Use uv instead."


def _insert_require_uv_block_rule(manager: LocalWorkflowDefinitionManager) -> None:
    _insert_rule(
        manager,
        "require-uv",
        RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when="variables.get('require_uv')",
            effects=[
                RuleEffect(
                    type="block",
                    tools=["Bash"],
                    command_pattern=REQUIRE_UV_COMMAND_PATTERN,
                    reason=REQUIRE_UV_REASON,
                ),
            ],
        ),
    )


class TestRequireUvBlockRule:
    """Tests for the require-uv block rule pattern."""

    @pytest.mark.asyncio
    async def test_blocks_bare_python(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None
        assert response.auto_approve is False

    @pytest.mark.asyncio
    async def test_blocks_shell_alias_via_normalized_bash(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "exec_command",
                "tool_input": {"command": "python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_passthrough_uv_command(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Commands already using uv should not block or rewrite."""
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "uv run python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "allow"
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_compound_command_blocks(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Compound commands should block instead of rewriting python/pip parts."""
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi && pip install foo"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_non_python_command_no_block(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-python Bash commands should not block or rewrite."""
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "allow"
        assert response.modified_input is None


class TestCompressBashOutputBundledRule:
    """Bundled compress-bash-output should cover shell CLIs and skip gcode."""

    @pytest.mark.asyncio
    async def test_codex_rewrites_bash_through_gsqz(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _sync_bundled_rules(db)

        row = manager.get_by_name("compress-bash-output")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None

        event = _make_event(
            data={"tool_name": "Bash", "tool_input": {"command": "echo ok"}},
            source=SessionSource.CODEX,
        )

        response = await RuleEngine(db).evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        assert response.modified_input is not None
        assert "gsqz" in response.modified_input["command"]
        assert "echo ok" in response.modified_input["command"]

    @pytest.mark.asyncio
    async def test_claude_still_rewrites_bash_through_gsqz(self, db: HubDatabase) -> None:
        _sync_bundled_rules(db)

        event = _make_event(
            data={"tool_name": "Bash", "tool_input": {"command": "echo ok"}},
            source=SessionSource.CLAUDE,
        )

        response = await RuleEngine(db).evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        assert response.modified_input is not None
        assert "gsqz" in response.modified_input["command"]
        assert "echo ok" in response.modified_input["command"]

    @pytest.mark.asyncio
    async def test_gcode_command_stays_unwrapped(self, db: HubDatabase) -> None:
        _sync_bundled_rules(db)

        event = _make_event(
            data={"tool_name": "Bash", "tool_input": {"command": "  gcode search Codex"}},
            source=SessionSource.CODEX,
        )

        response = await RuleEngine(db).evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        assert response.modified_input is None


class TestPermissionResponseEffects:
    """set_permission_response and set_retry should preserve explicit empty/false values."""

    @pytest.mark.asyncio
    async def test_permission_response_keeps_empty_payloads(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "permission-clear",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="set_permission_response",
                        permission_decision="allow",
                        input_updates={},
                        updated_permissions=[],
                    )
                ],
            ),
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            _make_event(data={"tool_name": "Read", "tool_input": {"file_path": "README.md"}}),
            session_id="sess-1",
            variables={},
        )

        assert response.decision == "allow"
        assert response.permission_decision == "allow"
        assert response.modified_input == {}
        assert response.updated_permissions == []

    @pytest.mark.asyncio
    async def test_set_retry_preserves_explicit_false(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "no-retry",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_retry", retry=False)],
            ),
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            _make_event(data={"tool_name": "Read", "tool_input": {"file_path": "README.md"}}),
            session_id="sess-1",
            variables={},
        )

        assert response.decision == "allow"
        assert response.retry is False
