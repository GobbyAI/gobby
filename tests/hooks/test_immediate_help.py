"""Immediate help preserves complete menus and pending work instructions."""

import json
from unittest.mock import MagicMock, patch

import pytest

from gobby.adapters import (
    AgyAdapter,
    ClaudeCodeAdapter,
    CodexHooksAdapter,
    DroidAdapter,
    GrokAdapter,
)
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.event_enrichment import EventEnricher
from gobby.hooks.events import HookEventType, HookResponse, SessionSource
from gobby.hooks.rule_evaluator import WorkflowRuleEvaluator
from gobby.hooks.skill_manager import HookSkillManager
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.capability_routing import gobby_help_prefix
from gobby.skills.parser import ParsedSkill
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager, get_skill_change_notifier
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.hooks.test_agent_events_coverage import _make_event, _TestHandler

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "source",
    [SessionSource(value) for value in ("claude", "codex", "droid", "grok", "qwen", "agy")],
)
@pytest.mark.parametrize("suffix", ["", " help"])
def test_first_and_repeated_help_defer_work(source: SessionSource, suffix: str) -> None:
    handler = _TestHandler()
    handler.skill_manager_mock.discover_core_skills.return_value = [
        ParsedSkill(name="custom", description="Project skill", content="PRIVATE BODY")
    ]
    prefix = "$gobby" if source == SessionSource.CODEX else "/gobby"
    event = _make_event(
        source=source,
        data={"prompt": prefix + suffix},
        metadata={"_platform_session_id": "session-1"},
    )
    workflow = MagicMock()
    evaluator = WorkflowRuleEvaluator(
        workflow_handler=workflow,
        dispatch_mcp_calls=MagicMock(),
        format_discovery_result=MagicMock(),
        database=None,
        logger=MagicMock(),
    )
    messages = MagicMock()
    enricher = EventEnricher(handler._session_manager, set(), messages)
    with (
        patch("gobby.workflows.state_manager.SessionVariableManager"),
        patch("gobby.skills.discovery.get_session_skill_exclusions", return_value=set()),
        patch.object(handler, "_inject_agent_instructions_if_needed") as inject,
    ):
        for _ in range(2):
            assert evaluator.evaluate(event) == (None, None)
            response = handler.handle_before_agent(event)
            enricher.enrich(event, response, workflow_context="HOUSEKEEPING")
            if source == SessionSource.GROK:
                assert response.decision == "block"
                content = response.reason
                assert GrokAdapter().translate_from_hook_response(
                    response, hook_type="user_prompt_submit"
                ) == {"decision": "block", "reason": content}
            else:
                assert response.decision == "allow"
                content = response.context
                assert content is not None
                assert "Make zero tool calls" in content
            assert content is not None
            assert f"{prefix} custom" in content
            assert f"{prefix} tasks" in content
            assert "HOUSEKEEPING" not in content
            assert "PRIVATE BODY" not in content
            assert "get_skill" not in content
            assert "list_skills" not in content
            assert ("/gobby" if prefix == "$gobby" else "$gobby") not in content
            adapters: dict[
                SessionSource,
                ClaudeCodeAdapter
                | CodexHooksAdapter
                | DroidAdapter
                | GrokAdapter
                | QwenAdapter
                | AgyAdapter,
            ] = {
                SessionSource.CLAUDE: ClaudeCodeAdapter(),
                SessionSource.CODEX: CodexHooksAdapter(),
                SessionSource.DROID: DroidAdapter(),
                SessionSource.GROK: GrokAdapter(),
                SessionSource.QWEN: QwenAdapter(),
                SessionSource.AGY: AgyAdapter(),
            }
            native = adapters[source].translate_from_hook_response(
                response,
                hook_type="PreInvocation" if source == SessionSource.AGY else "UserPromptSubmit",
            )
            assert f"{prefix} custom" in json.dumps(native)
        inject.assert_not_called()
        event.data["prompt"] = "Implement the change"
        handler.handle_before_agent(event)
        inject.assert_called_once()
    workflow.handle.assert_not_called()
    messages.get_undelivered_messages.assert_not_called()


@pytest.mark.parametrize(
    "count,description,overflow",
    [
        (0, "", False),
        (80, "Long description " * 200, False),
        (160, "界 " * 120, False),
        (400, "Long description " * 200, False),
        (2000, "", True),
    ],
)
@pytest.mark.parametrize("prefix", ["$gobby", "/gobby"])
def test_bounded_help_is_complete_or_explicit_overflow(
    count: int, description: str, overflow: bool, prefix: str
) -> None:
    handler = _TestHandler()
    handler._session_manager = None
    skills = [
        ParsedSkill(name=f"skill-{i:04}", description=description, content="") for i in range(count)
    ]
    handler.skill_manager_mock.discover_core_skills.return_value = skills
    content = handler._generate_help_content(command_prefix=prefix)
    assert len(content) <= 15_000
    assert len(content.encode("utf-8")) <= 15_000
    if overflow:
        assert "exceeds the display limit" in content
        assert f"{prefix} skills" in content
        assert "skill-0000" not in content
    else:
        assert "exceeds the display limit" not in content
        for skill in skills:
            assert f"`{prefix} {skill.name}`" in content
        for capability in load_capability_catalog().capabilities:
            assert f"`{prefix} {capability.name}`" in content


def test_description_keeps_dotted_words() -> None:
    handler = _TestHandler()
    handler._session_manager = None
    handler.skill_manager_mock.discover_core_skills.return_value = [
        ParsedSkill(name="csharp", description="C# and .NET standards. More detail.", content=""),
        ParsedSkill(
            name="moat", description="Manage .moat configuration. More detail.", content=""
        ),
    ]
    content = handler._generate_help_content()
    assert "C# and .NET standards." in content
    assert "Manage .moat configuration." in content
    assert "More detail" not in content


@pytest.mark.parametrize("prefix", ["$gobby", "/gobby"])
def test_description_examples_use_current_provider(prefix: str) -> None:
    handler = _TestHandler()
    handler._session_manager = None
    handler.skill_manager_mock.discover_core_skills.return_value = [
        ParsedSkill(
            name="review-example",
            description="Handle `$gobby review-example` or `/gobby review-example` requests.",
            content="",
        )
    ]
    content = handler._generate_help_content(command_prefix=prefix)
    assert content.count(f"`{prefix} review-example`") == 3
    assert ("$gobby" if prefix == "/gobby" else "/gobby") not in content


def test_help_database_failure_never_substitutes_a_partial_catalog() -> None:
    manager = HookSkillManager(db=MagicMock())
    with (
        patch.object(manager, "_load_from_db", side_effect=RuntimeError("offline")),
        patch.object(manager, "_load_from_filesystem") as fallback,
    ):
        with pytest.raises(RuntimeError, match="offline"):
            manager.discover_core_skills("project-a", require_complete=True)
    fallback.assert_not_called()


@pytest.mark.parametrize("prompt", ["$gobby tasks", "/gobby help me", "/gobbyish", None])
def test_help_classification_does_not_swallow_work(prompt: str | None) -> None:
    assert gobby_help_prefix(prompt) is None


def test_missing_manager_reports_unavailable_without_recovery() -> None:
    handler = _TestHandler()
    handler._skill_manager = None
    response = handler.handle_before_agent(_make_event(data={"prompt": "/gobby"}))
    assert isinstance(response, HookResponse)
    assert response.context is not None
    assert "Gobby help is unavailable." in response.context
    assert "get_skill" not in response.context


def test_project_menus_preserve_global_overrides_exclusions_and_scope_moves(
    temp_db: HubDatabase, isolated_checkout_factory: IsolatedCheckoutFactory
) -> None:
    gobby = isolated_checkout_factory(temp_db, "help-gobby").project.id
    goblins = isolated_checkout_factory(temp_db, "help-game-goblins").project.id
    storage = LocalSkillManager(temp_db, notifier=get_skill_change_notifier(temp_db))
    storage.create_skill(name="shared", description="Global description", content="")
    storage.create_skill(
        name="shared", description="Project override", content="", project_id=gobby
    )
    storage.create_skill(name="hidden", description="Excluded", content="", project_id=gobby)
    storage.create_skill(name="disabled", description="Disabled", content="", enabled=False)
    gusto = storage.create_skill(
        name="gusto", description="Gusto payroll", content="", project_id=gobby
    )
    manager = HookSkillManager(db=temp_db)
    handler = _TestHandler()
    handler._skill_manager = manager
    handler.session_manager_mock.db = temp_db
    with patch("gobby.skills.discovery.get_session_skill_exclusions", return_value={"hidden"}):
        before = handler._generate_help_content("session-1", project_id=gobby)
        assert "/gobby gusto" in before
        assert "Project override" in before
        assert "Global description" not in before
        assert "/gobby hidden" not in before
        assert "/gobby disabled" not in before
        assert "/gobby gusto" not in handler._generate_help_content(project_id=goblins)
        storage.move_to_project(gusto.id, goblins)
        after = handler._generate_help_content("session-1", project_id=gobby)
        other = handler._generate_help_content(project_id=goblins)
    assert "/gobby gusto" not in after
    assert "/gobby gusto" in other
    assert "Global description" in other
    assert "Project override" not in other


def test_help_uses_configured_content_limit() -> None:
    handler = _TestHandler()
    handler.skill_manager_mock.discover_core_skills.return_value = [
        ParsedSkill(name=f"entry-{i}", description="Long description " * 100, content="")
        for i in range(100)
    ]
    with patch("gobby.skills.authoring.resolve_bundled_max_content_size", return_value=3000):
        result = handler._generate_help_content()
    assert len(result.encode("utf-8")) <= 3000
    assert "exceeds the display limit" not in result
    assert "Long description" not in result
    for i in range(100):
        assert f"`/gobby entry-{i}`" in result


def test_help_stop_defers_housekeeping_and_preserves_first_work_injection() -> None:
    workflow = MagicMock()
    workflow.handle.return_value = HookResponse(decision="allow")
    evaluator = WorkflowRuleEvaluator(
        workflow_handler=workflow,
        dispatch_mcp_calls=MagicMock(),
        format_discovery_result=MagicMock(),
        database=MagicMock(),
        logger=MagicMock(),
    )
    event = _make_event(data={"prompt": "$gobby"}, metadata={"_platform_session_id": "session-1"})
    with patch("gobby.workflows.state_manager.SessionVariableManager") as variables:
        manager = variables.return_value
        manager.get_variables.return_value = {}
        assert evaluator.evaluate(event) == (None, None)
        manager.merge_variables.assert_called_once_with(
            "session-1",
            {
                "_current_user_prompt": "$gobby",
                "_agent_context_rehydrate_pending": True,
                "_help_deferred_activation": True,
            },
        )
        event.event_type = HookEventType.STOP
        manager.get_variables.return_value = {"_current_user_prompt": "$gobby"}
        assert evaluator.evaluate(event) == (None, None)
        workflow.handle.assert_not_called()
        manager.get_variables.return_value = {"_current_user_prompt": "Implement the feature"}
        assert evaluator.evaluate(event) == (None, None)
        workflow.handle.assert_called_once()
