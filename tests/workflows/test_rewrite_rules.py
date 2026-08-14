"""Tests for rewrite_input rules and require-uv block regression coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.adapters.claude_code import _ACTION_FIRST_PREFIXES, is_action_first_reason
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.workflows.definitions import (
    RuleDefinitionBody,
    RuleEffect,
    RuleTriggerEvent,
    split_rule_definition_data,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from gobby.workflows.templates import TemplateEngine
from tests.framing_corpus import (
    REDIRECT_RULES,
    TRUE_RESTRICTION_RULES,
)
from tests.framing_corpus import (
    SKILL_FETCH_REASON_TEMPLATE as _SKILL_FETCH_TEMPLATE,
)
from tests.framing_corpus import (
    bundled_before_tool_block_reasons as _bundled_before_tool_block_reasons,
)

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _insert_rule(
    manager: RuleDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
    enabled: bool = True,
) -> str:
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        priority=priority,
        enabled=enabled,
    )
    return row.id


def _sync_bundled_rules(db: HubDatabase) -> None:
    """Sync the real bundled rule set into the test database."""
    sync_bundled_rules(db, get_bundled_rules_path())


def _load_bundled_rule(
    manager: RuleDefinitionManager,
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
        rule_data = rules[rule_name]
        body_data, metadata = split_rule_definition_data(rule_data)
        body = RuleDefinitionBody.model_validate(body_data)
        priority = metadata.get("priority", 100)
        enabled = metadata.get("enabled", True)
        return _insert_rule(manager, rule_name, body, priority=priority, enabled=enabled)
    raise AssertionError(f"Bundled rule {rule_name!r} not found under {rules_path}")


class TestBundledBlockReasonFraming:
    def test_action_first_prefixes_match_frozen_contract(self) -> None:
        assert _ACTION_FIRST_PREFIXES == ("Retry ", "Use ", "Run ", "Call ", "Load ", "If ")

    def test_continue_opener_uses_true_restriction_framing(self) -> None:
        reason = "Continue only after a maintainer approves this restricted action."

        assert is_action_first_reason(reason) is False

    def test_every_live_before_tool_block_is_classified_once(self) -> None:
        reasons = _bundled_before_tool_block_reasons()

        assert REDIRECT_RULES.isdisjoint(TRUE_RESTRICTION_RULES)
        assert set(reasons) == REDIRECT_RULES | TRUE_RESTRICTION_RULES

    def test_redirect_reasons_open_with_frozen_action_marker(self) -> None:
        reasons = _bundled_before_tool_block_reasons()
        templated_skill_rules = {
            name
            for name, reason in reasons.items()
            if reason.lstrip().startswith("{{ skill_fetch_directive(")
        }

        for rule_name in sorted(REDIRECT_RULES - templated_skill_rules):
            reason = reasons[rule_name]
            assert is_action_first_reason(reason), f"{rule_name}: {reason!r}"
            assert not reason.lower().startswith(("do not", "blocked", "disabled"))

    def test_skill_fetch_template_is_the_only_marker_exception(self) -> None:
        reasons = _bundled_before_tool_block_reasons()

        assert reasons["require-claimed-task-required-skills"] == _SKILL_FETCH_TEMPLATE
        assert skill_fetch_directive("python").startswith("Load and fully read the skill")

    def test_critical_redirects_retain_literal_offset_zero_actions(self) -> None:
        reasons = _bundled_before_tool_block_reasons()

        assert reasons["require-code-index-skill"].startswith(
            '{{ skill_fetch_directive("code-index") }}'
        )
        assert reasons["require-java-skill"].startswith('{{ skill_fetch_directive("java") }}')
        assert reasons["no-invalid-git-flags"].startswith("Run the command without `--no-stat`")

    def test_bundled_skill_reasons_use_one_canonical_fetch_call(self) -> None:
        for reason in _bundled_before_tool_block_reasons().values():
            if "skill_fetch_directive(" not in reason:
                continue

            assert reason.count("skill_fetch_directive(") == 1
            assert 'call_tool("gobby-skills", "get_skill"' not in reason


class TestMCPRewriteNesting:
    """rewrite_input should auto-nest updates inside `arguments` for MCP call_tool."""

    @pytest.mark.asyncio
    async def test_rewrite_nests_inside_arguments(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "strip-flag",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
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
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

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
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """For native tools (not call_tool), updates should remain top-level."""
        _insert_rule(
            manager,
            "rewrite-command",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
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
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "allow"
        assert response.modified_input is not None
        assert response.modified_input["command"] == "uv run python script.py"
        assert "arguments" not in response.modified_input

    @pytest.mark.asyncio
    async def test_rewrite_mcp_string_arguments(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """When arguments is a JSON string, it should be parsed before merging."""
        _insert_rule(
            manager,
            "strip-flag",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
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
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        assert response.modified_input is not None
        inner = response.modified_input["arguments"]
        assert inner["skip_validation"] is False
        assert inner["task_id"] == "t-2"


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


REQUIRE_UV_COMMAND_PATTERN = (
    r"(^|(?<=[;&|]))\s*(?:sudo\s+)?"
    r"(?:pip3?\b|python(?:\d+(?:\.\d+)?)?\s+-m\s+pip\b)"
)
REQUIRE_UV_REASON = (
    "Use `uv pip …` or `uv run python -m pip …` — uv manages this project's Python environment."
)


def _insert_require_uv_block_rule(manager: RuleDefinitionManager) -> None:
    _insert_rule(
        manager,
        "require-uv",
        RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
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
    async def test_allows_bare_python(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.reason is None
        assert response.modified_input is None
        assert response.auto_approve is False

    @pytest.mark.asyncio
    async def test_allows_bare_python_via_normalized_exec_command(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "exec_command",
                "tool_input": {"command": "python script.py"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.modified_input is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "uv run python script.py",
            "uv pip install requests",
            "uv run python -m pip install requests",
        ],
    )
    async def test_passthrough_uv_command(
        self, db: HubDatabase, manager: RuleDefinitionManager, command: str
    ) -> None:
        """Commands already using uv should not block or rewrite."""
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_compound_command_blocks(
        self, db: HubDatabase, manager: RuleDefinitionManager
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
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_normalized_exec_command_blocks_python_module_pip(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "exec_command",
                "tool_input": {"command": "python3.13 -m pip install foo"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None
        assert response.auto_approve is False

    @pytest.mark.asyncio
    async def test_require_uv_false_bypasses_package_management_block(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_require_uv_block_rule(manager)

        event = _make_event(
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "pip install foo"},
            }
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": False}
        )

        assert response.decision == "allow"
        assert response.reason is None
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_non_python_command_no_block(
        self, db: HubDatabase, manager: RuleDefinitionManager
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
        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.modified_input is None


class TestPermissionResponseEffects:
    """set_permission_response and set_retry should preserve explicit empty/false values."""

    @pytest.mark.asyncio
    async def test_permission_response_keeps_empty_payloads(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "permission-clear",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
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
            session_id=SESSION_ID,
            variables={},
        )

        assert response.decision == "allow"
        assert response.permission_decision == "allow"
        assert response.modified_input == {}
        assert response.updated_permissions == []

    @pytest.mark.asyncio
    async def test_set_retry_preserves_explicit_false(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "no-retry",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_retry", retry=False)],
            ),
        )

        engine = RuleEngine(db)
        response = await engine.evaluate(
            _make_event(data={"tool_name": "Read", "tool_input": {"file_path": "README.md"}}),
            session_id=SESSION_ID,
            variables={},
        )

        assert response.decision == "allow"
        assert response.retry is False
