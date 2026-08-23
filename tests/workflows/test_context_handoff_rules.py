"""Tests for context-handoff rules.

Verifies context handoff rules sync correctly and have proper structure:
- clear-pending-context-reset-on-start: set_variable on session_start
- inject-clear-handoff-on-prompt: inject_context on turn_start (clear_self one-shot)
- inject-compact-handoff: inject_context on session_start
- inject-task-context-on-start: inject_context on session_start
- preserve-context-on-compact: set_variable on pre_compact (reset tracking vars)
- auto-compact-after-task-close: after_tool close_task compaction handoff

"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.codex_impl.item_normalization import build_tool_event_data
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT, HANDOFF_SUMMARY_INJECT_BUDGET
from gobby.sessions.compact_markers import COMPACT_SELF_INTERRUPT_WARNING
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observer_context_usage import (
    detect_context_compact_guidance,
    detect_mid_turn_context_compact_guidance,
)
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

CONTEXT_HANDOFF_RULES = {
    "clear-pending-context-reset-on-start",
    "inject-clear-handoff-on-prompt",
    "inject-compact-handoff",
    "inject-compact-handoff-on-prompt",
    "inject-task-context-on-start",
    "inject-wiki-overview",
    "preserve-context-on-compact",
    "nudge-compact-on-context-pressure",
    "nudge-compact-on-context-pressure-mid-turn",
    "auto-compact-after-task-close",
}
_FOSSIL_PREVIOUS_SESSION_SUMMARY = "inject-previous-session-summary"
_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/bundled_content_manifest.json"
)
SESSION_ID = "11111111-1111-4111-8111-111111111111"
_BUNDLED_RULES = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/rules"


def test_bundled_session_rules_do_not_advertise_wiki_ask() -> None:
    offenders: list[str] = []
    for path in sorted(_BUNDLED_RULES.rglob("*")):
        if path.suffix not in {".yaml", ".yml"}:
            continue
        if "deprecated" in path.relative_to(_BUNDLED_RULES).parts:
            continue
        text = path.read_text()
        if "wiki_ask" in text:
            offenders.append(path.relative_to(_BUNDLED_RULES).as_posix())
    assert offenders == []


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


class _SessionWithContextRatio:
    def __init__(self, ratio: float | None) -> None:
        self.context_usage_ratio: object = ratio
        self.context_used_tokens: object = None
        self.context_window: object = None


class _SessionManagerWithContextRatio:
    def __init__(self, ratio: float | None) -> None:
        self.ratio = ratio
        self.get_calls = 0

    def get(self, _session_id: str) -> _SessionWithContextRatio:
        self.get_calls += 1
        return _SessionWithContextRatio(self.ratio)


class TestContextHandoffSync:
    """Test that context-handoff rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All context-handoff rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        for rule_name in CONTEXT_HANDOFF_RULES:
            assert rule_name in rule_names, f"Missing rule: {rule_name}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All context-handoff rules should have group='context-handoff'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in CONTEXT_HANDOFF_RULES:
                body = row.definition_json
                assert body.get("group") == "context-handoff", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in CONTEXT_HANDOFF_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in {
                        "set_variable",
                        "inject_context",
                        "mcp_call",
                    }

    def test_session_start_rules_do_not_capture_baseline_via_mcp(self, db, manager) -> None:
        """Session start should rely on workflow lazy init, not an MCP capture rule."""
        _sync_bundled(db)

        session_start_rule_names = []
        for row in manager.list_all(enabled=True):
            body = RuleDefinitionBody.model_validate(row.definition_json)
            if body.event.value == "session_start":
                session_start_rule_names.append(row.name)

        assert "capture-baseline-dirty-files-on-start" not in session_start_rule_names

    def test_fossil_previous_session_summary_is_pruned(self, db, manager) -> None:
        """Template sync removes or disables the 0.4.x clear-injection fossil."""
        _sync_bundled(db)
        row = manager.get_by_name(_FOSSIL_PREVIOUS_SESSION_SUMMARY)
        assert row is None or not row.enabled

    def test_manifest_drops_fossil_and_lists_clear_handoff(self) -> None:
        files_map = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))["files"]
        assert (
            "workflows/rules/context-handoff/inject-previous-session-summary.yaml" not in files_map
        )
        assert "workflows/rules/context-handoff/inject-clear-handoff.yaml" in files_map


# ═══════════════════════════════════════════════════════════════════════
# clear-pending-context-reset-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestClearPendingContextResetOnStart:
    """Clear pending_context_reset flag on session_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("clear-pending-context-reset-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "pending_context_reset"

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("clear-pending-context-reset-on-start")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "pending_context_reset" in body.when

    def test_cleanup_runs_after_context_reset_consumers(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        cleanup = manager.get_by_name("clear-pending-context-reset-on-start")
        assert cleanup is not None
        assert cleanup.priority == 100

        for consumer_name in (
            "reset-progressive-discovery",
            "reset-skill-injection",
            "reset-memory-tracking-on-start",
        ):
            consumer = manager.get_by_name(consumer_name)
            assert consumer is not None
            assert consumer.priority < cleanup.priority


# ═══════════════════════════════════════════════════════════════════════
# inject-clear-handoff-on-prompt
# ═══════════════════════════════════════════════════════════════════════


class TestInjectClearHandoff:
    """Deliver the clear_self handoff once on the successor's first turn_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-clear-handoff-on-prompt")
        assert row is not None
        assert row.enabled
        assert row.priority == 11
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "clear_handoff_inject_pending" in body.when
        assert body.effects is not None
        effects = body.effects
        assert effects[0].type == "inject_context"
        template = effects[0].template
        assert template is not None
        assert "<!-- gobby:injected-context:begin -->" in template
        assert "<!-- gobby:injected-context:end -->" in template
        assert "Continuation Context (deliberate clear)" in template
        assert "Previous Session Context" not in template
        assert "Durable Tool-Call Evidence" not in template
        assert "Required Skill Reload" not in template
        assert "skill_fetch_batch_directive" not in template
        assert "handoff_summary_injectable" in template
        assert effects[1].type == "set_variable"
        assert effects[1].variable == "clear_handoff_inject_pending"
        assert effects[1].value is False

    @pytest.mark.asyncio
    async def test_prompt_path_injects_work_context_once(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        variables: dict[str, Any] = {
            "clear_handoff_inject_pending": True,
            "handoff_summary_injectable": "Clear continuation UNIQUE_CLEAR_HANDOFF",
            "mcp_calls": {"gobby-sessions": ["clear_self"]},
            "compact_resume_required_skills": ["tasks"],
        }
        first = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert first.context is not None
        assert "<!-- gobby:injected-context:begin -->" in first.context
        assert "Continuation Context (deliberate clear)" in first.context
        assert "Clear continuation UNIQUE_CLEAR_HANDOFF" in first.context
        assert "Previous Session Context" not in first.context
        assert "Durable Tool-Call Evidence" not in first.context
        assert "Required Skill Reload" not in first.context
        assert variables["clear_handoff_inject_pending"] is False

        second = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert not (second.context and "UNIQUE_CLEAR_HANDOFF" in second.context)
        assert not (second.context and "Continuation Context (deliberate clear)" in second.context)

    @pytest.mark.asyncio
    async def test_prompt_path_skips_when_pending_false(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        skipped = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "clear_handoff_inject_pending": False,
                "handoff_summary_injectable": "Should not inject UNIQUE_CLEAR_SKIP",
            },
        )
        assert not (skipped.context and "UNIQUE_CLEAR_SKIP" in skipped.context)
        assert not (
            skipped.context and "Continuation Context (deliberate clear)" in skipped.context
        )

    @pytest.mark.asyncio
    async def test_session_start_clear_does_not_inject_fossil_or_handoff(
        self, db: HubDatabase
    ) -> None:
        """A plain user /clear (no clear_self pending flag) injects no handoff block."""
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"source": "clear"},
        )
        result = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={"handoff_summary_injectable": "SHOULD_NOT_APPEAR UNIQUE_PLAIN_CLEAR"},
        )
        assert not (result.context and "Previous Session Context" in result.context)
        assert not (result.context and "SHOULD_NOT_APPEAR UNIQUE_PLAIN_CLEAR" in result.context)
        assert not (result.context and "Continuation Context (deliberate clear)" in result.context)


# ═══════════════════════════════════════════════════════════════════════
# inject-compact-handoff
# ═══════════════════════════════════════════════════════════════════════


class TestInjectCompactHandoff:
    """Inject compact handoff context after compaction."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-compact-handoff")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None
        assert "Continuation Context" in body.effects[0].template
        assert "Durable Tool-Call Evidence" in body.effects[0].template
        assert "mcp_calls" in body.effects[0].template
        assert "skill_fetch_batch_directive" in body.effects[0].template
        assert "Advisory Skill Reload" in body.effects[0].template
        assert "compact_resume_advisory_skills" in body.effects[0].template
        assert body.effects[1].type == "set_variable"
        assert body.effects[1].variable == "compact_resume_required_skills"
        assert body.effects[1].value == []
        assert body.effects[2].type == "set_variable"
        assert body.effects[2].variable == "compact_resume_advisory_skills"
        assert body.effects[2].value == []

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-compact-handoff")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "compact" in body.when

    @pytest.mark.asyncio
    async def test_durable_tool_call_evidence_reasserted_after_compaction(
        self, db: HubDatabase
    ) -> None:
        """Satisfy -> compact -> stop regression seam for stop-gate goal evaluators.

        A tool-invocation goal satisfied before compaction leaves its only
        durable evidence in the session's `mcp_calls` ledger; the transcript
        window the evaluator reads is truncated by the compaction. The
        post-compaction injection must re-assert that ledger so the evaluator
        sees the satisfied call and does not re-arm the goal (#20168).
        """
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"source": "compact"},
        )

        satisfied = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "session_summary": "Prior-window summary",
                "mcp_calls": {
                    "gobby-sessions": ["compact_self"],
                    "gobby-tasks": ["claim_task", "close_task"],
                },
            },
        )
        assert satisfied.context is not None
        assert "Durable Tool-Call Evidence" in satisfied.context
        assert "`gobby-sessions`: compact_self" in satisfied.context
        assert "`gobby-tasks`: claim_task, close_task" in satisfied.context

    @pytest.mark.asyncio
    async def test_unmet_tool_invocation_goal_gains_no_evidence(self, db: HubDatabase) -> None:
        """A tool never durably recorded must not appear as evidence."""
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"source": "compact"},
        )

        uncalled = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "session_summary": "Prior-window summary",
                "mcp_calls": {"gobby-tasks": ["claim_task"]},
            },
        )
        assert uncalled.context is not None
        assert "compact_self" not in uncalled.context

        empty_ledger = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={"session_summary": "Prior-window summary"},
        )
        assert not (empty_ledger.context and "Durable Tool-Call Evidence" in empty_ledger.context)

    def test_prompt_rule_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-compact-handoff-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "compact_handoff_inject_pending" in body.when
        assert body.effects is not None
        effects = body.effects
        assert effects[0].type == "inject_context"
        template = effects[0].template
        assert template is not None
        assert "<!-- gobby:injected-context:begin -->" in template
        assert "<!-- gobby:injected-context:end -->" in template
        assert "Continuation Context" in template
        assert "wiki_overview" not in template
        assert "user_profile_content" in template
        assert "task_context" in template
        assert effects[1].type == "set_variable"
        assert effects[1].variable == "compact_handoff_inject_pending"
        assert effects[1].value is False
        assert effects[2].variable == "pending_context_reset"
        assert effects[2].value is False

    @pytest.mark.asyncio
    async def test_prompt_path_injects_handoff_wiki_profile_task_and_skills(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        first = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "compact_handoff_inject_pending": True,
                "plan_mode": True,
                "session_summary": "Grok compact continuation UNIQUE_SUMMARY",
                "mcp_calls": {
                    "gobby-sessions": ["compact_self"],
                    "gobby-tasks": ["claim_task"],
                },
                "compact_resume_required_skills": ["tasks"],
                "compact_resume_advisory_skills": ["restraint"],
                "wiki_overview": "Wiki UNIQUE_WIKI",
                "user_profile_content": "Profile UNIQUE_PROFILE",
                "task_context": "Task UNIQUE_TASK",
            },
        )
        assert first.context is not None
        assert "<!-- gobby:injected-context:begin -->" in first.context
        assert "Grok compact continuation UNIQUE_SUMMARY" in first.context
        assert "Durable Tool-Call Evidence" in first.context
        assert "`gobby-sessions`: compact_self" in first.context
        assert "Required Skill Reload" in first.context
        assert '{"name":"tasks"}' in first.context
        assert "Advisory Skill Reload" in first.context
        assert "`restraint`" in first.context
        assert first.context.count("Wiki UNIQUE_WIKI") == 1
        assert "Profile UNIQUE_PROFILE" in first.context
        assert "Task UNIQUE_TASK" in first.context

    @pytest.mark.asyncio
    async def test_prompt_path_fits_grok_additional_context_budget(self, db: HubDatabase) -> None:
        """Compact reinjection leaves room for the first-prompt agent preamble."""
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        result = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "compact_handoff_inject_pending": True,
                "handoff_summary_injectable": "s" * HANDOFF_SUMMARY_INJECT_BUDGET,
                "mcp_calls": {
                    "gobby-memory": ["create_memory", "search_memories"],
                    "gobby-sessions": ["compact_self", "get_session"],
                    "gobby-tasks": ["claim_task", "close_task", "get_task"],
                },
                "compact_resume_required_skills": [
                    "loading-skills",
                    "tasks",
                    "development-discipline",
                    "code-index",
                ],
                "compact_resume_advisory_skills": ["brevity"],
                "wiki_overview": "w" * 300,
                "task_context": "t" * 150,
            },
        )

        assert result.context is not None
        first_prompt_agent_preamble = "p" * 2_500
        aggregate = f"{first_prompt_agent_preamble}\n\n{result.context}"
        assert len(aggregate) <= ADDITIONAL_CONTEXT_LIMIT

    @pytest.mark.asyncio
    async def test_prompt_path_omits_profile_for_spawned_agent(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        spawned = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "compact_handoff_inject_pending": True,
                "plan_mode": True,
                "is_spawned_agent": True,
                "session_summary": "Spawned continuation UNIQUE_SPAWNED",
                "user_profile_content": "Profile UNIQUE_SPAWNED_PROFILE",
                "wiki_overview": "Wiki UNIQUE_SPAWNED_WIKI",
            },
        )
        assert spawned.context is not None
        assert "Spawned continuation UNIQUE_SPAWNED" in spawned.context
        # Taskless spawned agents also skip the wiki overview (#20451).
        assert "Wiki UNIQUE_SPAWNED_WIKI" not in spawned.context
        assert "Profile UNIQUE_SPAWNED_PROFILE" not in spawned.context
        assert "## Global User Profile" not in spawned.context

        tasked = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "compact_handoff_inject_pending": True,
                "plan_mode": True,
                "is_spawned_agent": True,
                "task_claimed": True,
                "session_summary": "Spawned continuation UNIQUE_SPAWNED",
                "user_profile_content": "Profile UNIQUE_SPAWNED_PROFILE",
                "wiki_overview": "Wiki UNIQUE_SPAWNED_WIKI",
            },
        )
        assert tasked.context is not None
        assert "Wiki UNIQUE_SPAWNED_WIKI" in tasked.context
        assert "Profile UNIQUE_SPAWNED_PROFILE" not in tasked.context

    @pytest.mark.asyncio
    async def test_prompt_path_skips_when_pending_false(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        skipped = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "compact_handoff_inject_pending": False,
                "plan_mode": True,
                "session_summary": "Should not inject UNIQUE_SKIP",
            },
        )
        assert not (skipped.context and "UNIQUE_SKIP" in skipped.context)
        assert not (skipped.context and "Continuation Context" in skipped.context)

    @pytest.mark.asyncio
    async def test_prompt_path_is_one_shot(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        variables: dict[str, Any] = {
            "compact_handoff_inject_pending": True,
            "pending_context_reset": True,
            "plan_mode": True,
            "session_summary": "One-shot UNIQUE_ONCE",
            "compact_resume_required_skills": ["tasks"],
            "compact_resume_advisory_skills": ["restraint"],
        }
        first = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert first.context is not None
        assert "One-shot UNIQUE_ONCE" in first.context
        assert variables["compact_handoff_inject_pending"] is False
        assert variables["pending_context_reset"] is False
        assert variables["compact_resume_required_skills"] == []
        assert variables["compact_resume_advisory_skills"] == []

        second = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert not (second.context and "One-shot UNIQUE_ONCE" in second.context)
        assert not (second.context and "Continuation Context" in second.context)

    @pytest.mark.asyncio
    async def test_prompt_path_excludes_skills_reloaded_this_epoch(self, db: HubDatabase) -> None:
        """Skills already back in the epoch ledger are not directed again."""
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={},
        )
        result = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "compact_handoff_inject_pending": True,
                "session_summary": "Epoch continuation UNIQUE_EPOCH",
                "loaded_skills": ["tasks", "restraint"],
                "compact_resume_required_skills": ["tasks", "python"],
                "compact_resume_advisory_skills": ["restraint"],
            },
        )
        assert result.context is not None
        assert "Required Skill Reload" in result.context
        assert '{"name":"python"}' in result.context
        assert '{"name":"tasks"}' not in result.context
        assert "Advisory Skill Reload" not in result.context

    @pytest.mark.asyncio
    async def test_session_start_reload_list_survives_stale_ledger(self, db: HubDatabase) -> None:
        """The pre-compact ledger never suppresses reloads for the fresh epoch.

        reset-skill-injection must clear loaded_skills before the handoff
        template renders, so skills genuinely lost to the context reset are
        still directed for reload.
        """
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"source": "compact"},
        )
        variables: dict[str, Any] = {
            "session_summary": "Fresh epoch UNIQUE_FRESH",
            "loaded_skills": ["tasks", "python"],
            "compact_resume_required_skills": ["tasks", "python"],
        }
        result = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert result.context is not None
        assert "Required Skill Reload" in result.context
        assert '{"name":"tasks"}' in result.context
        assert '{"name":"python"}' in result.context
        assert variables["loaded_skills"] == []

    def test_ledger_reset_precedes_handoff_injection(self, db, manager) -> None:
        _sync_bundled(db)
        inject = manager.get_by_name("inject-compact-handoff")
        reset = manager.get_by_name("reset-skill-injection")
        assert inject is not None
        assert reset is not None
        assert reset.priority < inject.priority


# ═══════════════════════════════════════════════════════════════════════
# inject-wiki-overview
# ═══════════════════════════════════════════════════════════════════════


class TestInjectWikiOverview:
    """Inject the project wiki overview once per context epoch at first prompt (#17520)."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-wiki-overview")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects is not None
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None
        assert "Project Wiki" in body.effects[0].template
        assert "gobby:injected-context:begin" in body.effects[0].template
        assert body.effects[1].type == "set_variable"
        assert body.effects[1].variable == "wiki_overview_injected"
        assert body.effects[1].value is True

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-wiki-overview")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "wiki_overview" in body.when
        assert "not variables.get('wiki_overview_injected')" in body.when

    @pytest.mark.asyncio
    async def test_injects_overview_once_per_epoch(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "first prompt"},
        )

        seeded = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={"wiki_overview": "Totals: 22 concepts · 196 sources"},
        )
        assert seeded.context is not None
        assert "Project Wiki" in seeded.context
        assert "Totals: 22 concepts · 196 sources" in seeded.context

        gated = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "wiki_overview": "Totals: 22 concepts · 196 sources",
                "wiki_overview_injected": True,
            },
        )
        assert not (gated.context and "Project Wiki" in gated.context)

        unseeded = await engine.evaluate(event, session_id=SESSION_ID, variables={})
        assert not (unseeded.context and "Project Wiki" in unseeded.context)

    @pytest.mark.asyncio
    async def test_taskless_spawned_agent_gets_no_wiki_block(self, db) -> None:
        """Spawned reviewers never query the wiki; the block waits for task work (#20451)."""
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"prompt": "review this plan"},
        )
        base = {"wiki_overview": "Totals: 22 concepts", "is_spawned_agent": True}

        taskless = await engine.evaluate(event, session_id=SESSION_ID, variables=dict(base))
        assert not (taskless.context and "Project Wiki" in taskless.context)

        with_task = await engine.evaluate(
            event, session_id=SESSION_ID, variables={**base, "task_claimed": True}
        )
        assert with_task.context is not None
        assert "Project Wiki" in with_task.context

        auto_task = await engine.evaluate(
            event, session_id=SESSION_ID, variables={**base, "auto_task_ref": "#7"}
        )
        assert auto_task.context is not None
        assert "Project Wiki" in auto_task.context


# ═══════════════════════════════════════════════════════════════════════
# inject-task-context-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestInjectTaskContextOnStart:
    """Inject active task context on session_start (not resume)."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-task-context-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects is not None
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-task-context-on-start")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "resume" in body.when


def test_authoritative_sync_retires_prepare_clear_handoff(db, manager) -> None:
    """Removing the bundled template soft-deletes the installed rule."""
    manager.create(
        name="prepare-clear-handoff",
        definition_json=json.dumps(
            {
                "event": "turn_start",
                "effects": [
                    {
                        "type": "set_variable",
                        "variable": "handoff_source",
                        "value": "clear",
                    }
                ],
            }
        ),
        enabled=True,
        source="installed",
        tags=["gobby", "context-handoff"],
    )

    result = _sync_bundled(db)

    assert result["orphaned"] >= 1
    assert manager.get_by_name("prepare-clear-handoff") is None
    retired = manager.get_by_name("prepare-clear-handoff", include_deleted=True)
    assert retired is not None
    assert retired.deleted_at is not None
    assert retired.enabled is True


# ═══════════════════════════════════════════════════════════════════════
# preserve-context-on-compact (multi-effect)
# ═══════════════════════════════════════════════════════════════════════


class TestPreserveContextOnCompact:
    """Reset tracking variables before compaction."""

    def test_event_is_pre_compact(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "pre_compact"

    def test_has_eleven_effects(self, db, manager) -> None:
        """Should have 11 set_variable effects (incl. reminder cadence markers)."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects
        assert len(effects) == 11
        assert all(e.type == "set_variable" for e in effects)

    def test_resets_injected_memory_ids(self, db, manager) -> None:
        """Should reset injected_memory_ids to empty list."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects
        memory_reset = [
            e for e in effects if e.type == "set_variable" and e.variable == "injected_memory_ids"
        ]
        assert len(memory_reset) == 1
        assert memory_reset[0].value == []

    def test_sets_pending_context_reset(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Should set pending_context_reset to true."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects
        reset_flag = [
            e for e in effects if e.type == "set_variable" and e.variable == "pending_context_reset"
        ]
        assert len(reset_flag) == 1
        assert reset_flag[0].value is True

    def test_resets_context_guidance_epoch(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects
        compacted_turn = [
            e
            for e in effects
            if e.type == "set_variable" and e.variable == "last_compacted_turn_seq"
        ]
        turns_since = [
            e for e in effects if e.type == "set_variable" and e.variable == "turns_since_compact"
        ]
        mid_turn_band = [
            e
            for e in effects
            if e.type == "set_variable" and e.variable == "context_compact_mid_turn_pressure_band"
        ]
        shown_kinds = [
            e
            for e in effects
            if e.type == "set_variable" and e.variable == "context_compact_guidance_shown_kinds"
        ]
        consider_guard = [
            e
            for e in effects
            if e.type == "set_variable" and e.variable == "gobby_plan_consider_shown"
        ]
        assert len(compacted_turn) == 1
        assert compacted_turn[0].value == "variables.get('parent_turn_seq') or 0"
        assert len(turns_since) == 1
        assert turns_since[0].value == 0
        assert len(mid_turn_band) == 1
        assert mid_turn_band[0].value == "none"
        assert len(shown_kinds) == 1
        assert shown_kinds[0].value == []
        assert len(consider_guard) == 1
        assert consider_guard[0].value is False


class TestNudgeCompactOnContextPressure:
    """Compact guidance rule and observer behavior."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("nudge-compact-on-context-pressure")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when == "variables.get('context_compact_guidance_message')"
        assert body.effects is not None
        assert body.effects[0].type == "inject_context"
        assert "context_compact_guidance_message" in (body.effects[0].template or "")

        mid_turn_row = manager.get_by_name("nudge-compact-on-context-pressure-mid-turn")
        assert mid_turn_row is not None
        mid_turn_body = RuleDefinitionBody.model_validate(mid_turn_row.definition_json)
        assert mid_turn_body.event.value == "after_tool"
        assert mid_turn_body.when == body.when
        assert mid_turn_body.effects == body.effects

    async def test_turn_start_observer_sets_guidance_and_rule_injects_context(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.workflows.state_manager import SessionVariableManager

        _sync_bundled(db)
        handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db),
            session_manager=_SessionManagerWithContextRatio(0.70),
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "continue"},
            cwd=".",
            metadata={"_platform_session_id": SESSION_ID},
        )

        response = await handler._evaluate_rules(event)

        variables = SessionVariableManager(db).get_variables(SESSION_ID)
        guidance = variables["context_compact_guidance_message"]
        assert "Context pressure is 70%" in guidance
        assert response.context is not None
        assert guidance in response.context

    async def test_mid_turn_threshold_crossings_inject_once_each(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.workflows.state_manager import SessionVariableManager

        _sync_bundled(db)
        session_manager = _SessionManagerWithContextRatio(0.39)
        handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db),
            session_manager=session_manager,
        )
        variable_manager = SessionVariableManager(db)
        variable_manager.merge_variables(
            SESSION_ID,
            {"parent_turn_seq": 15, "chat_mode": "normal"},
        )
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "functions.exec_command",
                "tool_input": {"cmd": "true"},
                "tool_output": {"exit_code": 0},
            },
            cwd=".",
            metadata={"_platform_session_id": SESSION_ID},
        )

        injections: list[str] = []
        for ratio in (0.39, 0.40, 0.55, 0.69, 0.70, 0.80):
            session_manager.ratio = ratio
            response = await handler._evaluate_rules(event)
            if response.context is not None:
                injections.append(response.context)

        assert len(injections) == 2
        assert (
            "Context pressure is 40%. Consider calling `gobby-sessions:compact_self` "
            "at the next natural pause in your work."
        ) in injections[0]
        assert "Context pressure is 70%" in injections[1]
        variables = variable_manager.get_variables(SESSION_ID)
        assert variables["parent_turn_seq"] == 15
        assert variables["context_compact_mid_turn_pressure_band"] == "strong"
        assert variables["context_compact_guidance_shown_kinds"] == ["soft", "strong"]

    @pytest.mark.parametrize(
        "source",
        [
            SessionSource.CLAUDE,
            SessionSource.CODEX,
            SessionSource.QWEN,
            SessionSource.GROK,
            SessionSource.DROID,
        ],
    )
    async def test_pending_reset_suppresses_after_tool_guidance_across_providers(
        self,
        db: HubDatabase,
        source: SessionSource,
    ) -> None:
        from gobby.workflows.state_manager import SessionVariableManager

        _sync_bundled(db)
        session_manager = _SessionManagerWithContextRatio(0.90)
        handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db),
            session_manager=session_manager,
        )
        variable_manager = SessionVariableManager(db)
        variable_manager.merge_variables(
            SESSION_ID,
            {
                "chat_mode": "normal",
                "pending_context_reset": True,
                "context_compact_guidance_kind": "strong",
                "context_compact_guidance_message": "stale guidance",
                "context_compact_mid_turn_pressure_band": "none",
                "context_compact_guidance_shown_kinds": [],
            },
        )
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=source,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/src/module.py"},
                "tool_output": "contents",
            },
            cwd=".",
            metadata={"_platform_session_id": SESSION_ID},
        )

        response = await handler._evaluate_rules(event)

        variables = variable_manager.get_variables(SESSION_ID)
        assert "Context pressure" not in (response.context or "")
        assert session_manager.get_calls == 0
        assert variables["context_compact_guidance_kind"] == ""
        assert variables["context_compact_guidance_message"] == ""
        assert variables["context_compact_mid_turn_pressure_band"] == "none"
        assert variables["context_compact_guidance_shown_kinds"] == []

    async def test_codex_interrupted_compaction_suppresses_trailing_guidance_until_start(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.workflows.state_manager import SessionVariableManager

        _sync_bundled(db)
        session_manager = _SessionManagerWithContextRatio(0.90)
        handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db),
            session_manager=session_manager,
        )
        variable_manager = SessionVariableManager(db)
        variable_manager.merge_variables(
            SESSION_ID,
            {
                "chat_mode": "normal",
                "parent_turn_seq": 15,
                "context_compact_guidance_kind": "strong",
                "context_compact_guidance_message": "stale guidance",
                "context_compact_mid_turn_pressure_band": "strong",
                "context_compact_guidance_shown_kinds": ["soft", "strong"],
            },
        )

        def event(event_type: HookEventType, data: dict[str, Any]) -> HookEvent:
            return HookEvent(
                event_type=event_type,
                session_id=SESSION_ID,
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data=data,
                cwd=".",
                metadata={"_platform_session_id": SESSION_ID},
            )

        await handler._evaluate_rules(event(HookEventType.PRE_COMPACT, {"trigger": "manual"}))

        variables = variable_manager.get_variables(SESSION_ID)
        assert variables["pending_context_reset"] is True
        assert variables["context_compact_mid_turn_pressure_band"] == "none"
        assert variables["context_compact_guidance_shown_kinds"] == []

        trailing_response = await handler._evaluate_rules(
            event(
                HookEventType.AFTER_TOOL,
                {
                    "tool_name": "mcp__gobby__call_tool",
                    "tool_input": {
                        "server_name": "gobby-sessions",
                        "tool_name": "compact_self",
                        "arguments": {},
                    },
                    "tool_output": {"success": False, "error": "interrupted"},
                },
            )
        )

        variables = variable_manager.get_variables(SESSION_ID)
        assert "Context pressure" not in (trailing_response.context or "")
        assert session_manager.get_calls == 0
        assert variables["context_compact_guidance_kind"] == ""
        assert variables["context_compact_guidance_message"] == ""
        assert variables["context_compact_mid_turn_pressure_band"] == "none"
        assert variables["context_compact_guidance_shown_kinds"] == []

        await handler._evaluate_rules(event(HookEventType.SESSION_START, {}))

        variables = variable_manager.get_variables(SESSION_ID)
        assert variables["pending_context_reset"] is False

        after_tool = event(
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "functions.exec_command",
                "tool_input": {"cmd": "true"},
                "tool_output": {"exit_code": 0},
            },
        )
        session_manager.ratio = 0.40
        soft_response = await handler._evaluate_rules(after_tool)
        session_manager.ratio = 0.70
        strong_response = await handler._evaluate_rules(after_tool)

        assert "Context pressure is 40%" in (soft_response.context or "")
        assert "Context pressure is 70%" in (strong_response.context or "")
        variables = variable_manager.get_variables(SESSION_ID)
        assert variables["context_compact_mid_turn_pressure_band"] == "strong"
        assert variables["context_compact_guidance_shown_kinds"] == ["soft", "strong"]

    async def test_spawned_plan_mode_suppresses_soft_and_strong_guidance(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.workflows.state_manager import SessionVariableManager

        _sync_bundled(db)
        session_manager = _SessionManagerWithContextRatio(0.40)
        handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db),
            session_manager=session_manager,
        )
        variable_manager = SessionVariableManager(db)
        variable_manager.merge_variables(
            SESSION_ID,
            {
                "chat_mode": "plan",
                "is_spawned_agent": True,
                "mode_level": 0,
                "parent_turn_seq": 4,
                "plan_mode": True,
            },
        )
        turn_start = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"permission_mode": "plan", "prompt": "continue planning"},
            cwd=".",
            metadata={"_platform_session_id": SESSION_ID},
        )

        response = await handler._evaluate_rules(turn_start)

        assert "Context pressure" not in (response.context or "")
        variables = variable_manager.get_variables(SESSION_ID)
        assert variables["context_compact_guidance_message"] == ""
        assert "turns_since_compact" not in variables

        session_manager.ratio = 0.70
        after_tool = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "permission_mode": "plan",
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/src/module.py"},
                "tool_output": "contents",
            },
            cwd=".",
            metadata={"_platform_session_id": SESSION_ID},
        )

        response = await handler._evaluate_rules(after_tool)

        assert "Context pressure" not in (response.context or "")
        variables = variable_manager.get_variables(SESSION_ID)
        assert variables["context_compact_guidance_message"] == ""
        assert variables["context_compact_mid_turn_pressure_band"] == "none"
        assert variables["context_compact_guidance_shown_kinds"] == []

    def test_soft_nudge_at_forty_percent(self) -> None:
        variables = {"parent_turn_seq": 4, "chat_mode": "normal"}
        session_manager = _SessionManagerWithContextRatio(0.40)

        detect_context_compact_guidance(variables, "session-1", session_manager)

        assert variables["context_compact_guidance_kind"] == "soft"
        assert variables["context_compact_guidance_message"] == (
            "Context pressure is 40%. Consider calling `gobby-sessions:compact_self` "
            "at the next natural pause in your work."
        )
        assert variables["context_compact_guidance_shown_kinds"] == ["soft"]

    def test_strong_nudge_is_emitted_once_per_compaction_epoch(self) -> None:
        variables = {"parent_turn_seq": 8, "chat_mode": "normal"}
        session_manager = _SessionManagerWithContextRatio(0.9)

        detect_context_compact_guidance(variables, "session-1", session_manager)

        assert variables["context_compact_guidance_kind"] == "strong"
        assert "90%" in variables["context_compact_guidance_message"]
        assert variables["context_compact_guidance_shown_kinds"] == ["strong"]

        variables["parent_turn_seq"] = 9
        detect_context_compact_guidance(variables, "session-1", session_manager)

        assert variables["context_compact_guidance_message"] == ""

    def test_plan_mode_skips_guidance(self) -> None:
        variables = {"parent_turn_seq": 1, "chat_mode": "plan"}
        session_manager = _SessionManagerWithContextRatio(0.9)

        detect_context_compact_guidance(variables, "session-1", session_manager)

        assert variables["context_compact_guidance_message"] == ""
        assert "turns_since_compact" not in variables

    def test_unknown_usage_fallback_after_ten_non_plan_turns(self) -> None:
        variables = {"parent_turn_seq": 9, "chat_mode": "normal", "turns_since_compact": 9}

        detect_context_compact_guidance(
            variables, "session-1", _SessionManagerWithContextRatio(None)
        )

        assert variables["context_compact_guidance_kind"] == "unknown"
        assert "unknown for 10 non-plan turns" in variables["context_compact_guidance_message"]
        assert variables["context_compact_guidance_shown_kinds"] == ["unknown"]

        variables["parent_turn_seq"] = 10
        detect_context_compact_guidance(
            variables, "session-1", _SessionManagerWithContextRatio(None)
        )

        assert variables["context_compact_guidance_message"] == ""


# ═══════════════════════════════════════════════════════════════════════
# auto-compact-after-task-close
# ═══════════════════════════════════════════════════════════════════════


class TestAutoCompactAfterTaskClose:
    """Compact web chat or nudge terminal after closing one task with substantial work left."""

    def test_event_and_effects(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("auto-compact-after-task-close")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "after_tool"
        effects = body.resolved_effects
        assert len(effects) == 3

        compact_calls = [effect for effect in effects if effect.type == "mcp_call"]
        dedupe_sets = [
            effect
            for effect in effects
            if effect.type == "set_variable"
            and effect.variable == "_auto_compact_after_task_close_queued_for"
        ]
        fallback_nudges = [effect for effect in effects if effect.type == "inject_context"]
        assert len(compact_calls) == 1
        assert len(dedupe_sets) == 1
        assert len(fallback_nudges) == 1

        compact_call = compact_calls[0]
        assert compact_call.server == "gobby-sessions"
        assert compact_call.tool == "compact_self"
        assert compact_call.background is True
        assert compact_call.when is None
        assert compact_call.arguments == {"rule_name": "auto-compact-after-task-close"}

        fallback_nudge = fallback_nudges[0]
        assert "compact_call_queue_failed" in (fallback_nudge.when or "")
        assert fallback_nudge.template is not None

        assert "compact_self" in fallback_nudge.template
        fallback_template = " ".join(fallback_nudge.template.split())
        assert COMPACT_SELF_INTERRUPT_WARNING in fallback_template

    def test_condition_references_close_task_and_helpers(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("auto-compact-after-task-close")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "gobby-tasks" in body.when
        assert "close_task" in body.when
        assert "tool_call_succeeded()" in body.when
        assert "task_type_in" in body.when
        assert "claimed_tasks" in body.when
        assert "closed" in body.when
        assert "_auto_compact_after_task_close_queued_for" in body.when

    @pytest.mark.asyncio
    async def test_blocked_close_task_preview_does_not_queue_compaction(self, db) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#123", "preview": True},
                },
                "tool_output": {
                    "success": True,
                    "preview": True,
                    "can_close": False,
                    "closed": False,
                },
            },
            metadata={"session_type": "terminal"},
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"claimed_tasks": {"task-a": {"task_type": "task"}}},
        )

        assert response.metadata.get("mcp_calls", []) == []

    @pytest.mark.asyncio
    async def test_terminal_conditional_close_queues_compact_self_once(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = {
            "claimed_tasks": {
                "task-a": {"task_type": "task"},
                "task-b": {"task_type": "task"},
            }
        }
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {
                        "task_id": "#123",
                        "commit_sha": "abc123",
                        "preview": True,
                    },
                },
                "tool_output": {
                    "success": True,
                    "result": {
                        "preview": True,
                        "can_close": True,
                        "closed": True,
                    },
                },
            },
            metadata={"session_type": "terminal"},
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        compact_calls = [
            call
            for call in response.metadata.get("mcp_calls", [])
            if call["server"] == "gobby-sessions" and call["tool"] == "compact_self"
        ]
        assert len(compact_calls) == 1
        assert compact_calls[0]["background"] is True
        assert compact_calls[0]["arguments"] == {"rule_name": "auto-compact-after-task-close"}
        assert response.context is None
        assert variables["_auto_compact_after_task_close_queued_for"] == "#123"

        second_response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        second_compact_calls = [
            call
            for call in second_response.metadata.get("mcp_calls", [])
            if call["server"] == "gobby-sessions" and call["tool"] == "compact_self"
        ]
        assert second_compact_calls == []


def test_plan_mode_resets_pressure_band() -> None:
    variables: dict[str, Any] = {"chat_mode": "normal"}
    session_manager = _SessionManagerWithContextRatio(0.9)

    detect_mid_turn_context_compact_guidance(variables, "session-1", session_manager)
    assert variables["context_compact_guidance_kind"] == "strong"

    variables["chat_mode"] = "plan"
    detect_mid_turn_context_compact_guidance(variables, "session-1", session_manager)

    assert variables["context_compact_mid_turn_pressure_band"] == "none"
    assert variables["context_compact_guidance_shown_kinds"] == []

    variables["chat_mode"] = "normal"
    detect_mid_turn_context_compact_guidance(variables, "session-1", session_manager)

    assert variables["context_compact_guidance_kind"] == "strong"
    assert "Context pressure is 90%" in variables["context_compact_guidance_message"]


# ═══════════════════════════════════════════════════════════════════════
# auto-compact-after-task-close: per-CLI native payload coverage (#20813)
# ═══════════════════════════════════════════════════════════════════════

_CLOSE_PAYLOAD_TEXT = json.dumps({"success": True, "result": {"closed": True, "task_id": "#42"}})
_WRAPPER_INPUT: dict[str, Any] = {
    "server_name": "gobby-tasks",
    "tool_name": "close_task",
    "arguments": {"task_id": "#42", "commit_sha": "abc123"},
}
_MCP_RESULT_ENVELOPE: dict[str, Any] = {
    "content": [{"type": "text", "text": _CLOSE_PAYLOAD_TEXT}],
    "isError": False,
}


def _claude_close_event(tool_response: Any) -> HookEvent | None:
    return ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": SESSION_ID,
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": dict(_WRAPPER_INPUT),
                "tool_response": tool_response,
            },
        }
    )


def _codex_app_server_close_event() -> HookEvent:
    data = build_tool_event_data(
        {
            "id": "item-1",
            "type": "mcpToolCall",
            "server": "gobby",
            "tool": "call_tool",
            "arguments": dict(_WRAPPER_INPUT),
            "result": dict(_MCP_RESULT_ENVELOPE),
            "status": "completed",
        }
    )
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"session_type": "terminal"},
    )


def _codex_hooks_close_event() -> HookEvent | None:
    return CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PostToolUse",
            "input_data": {
                "session_id": SESSION_ID,
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": dict(_WRAPPER_INPUT),
                "tool_response": dict(_MCP_RESULT_ENVELOPE),
            },
        }
    )


def _grok_close_event(tool_result: Any) -> HookEvent | None:
    return GrokAdapter().translate_to_hook_event(
        {
            "source": "grok",
            "hook_type": "post_tool_use",
            "input_data": {
                "sessionId": SESSION_ID,
                "toolName": "mcp__gobby__call_tool",
                "toolInput": dict(_WRAPPER_INPUT),
                "toolResult": tool_result,
            },
        }
    )


def _qwen_close_event() -> HookEvent | None:
    return QwenAdapter().translate_to_hook_event(
        {
            "hook_type": "PostToolUse",
            "input_data": {
                "session_id": SESSION_ID,
                "tool_name": "call_tool",
                "tool_input": dict(_WRAPPER_INPUT),
                "tool_response": dict(_MCP_RESULT_ENVELOPE),
            },
        }
    )


def _droid_close_event() -> HookEvent | None:
    return DroidAdapter().translate_to_hook_event(
        {
            "hook_type": "PostToolUse",
            "input_data": {
                "session_id": SESSION_ID,
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": dict(_WRAPPER_INPUT),
                "tool_response": dict(_MCP_RESULT_ENVELOPE),
            },
        }
    )


class TestAutoCompactAfterTaskCloseAcrossClis:
    """The rule condition fires on every supported CLI's native after-tool payload.

    Each case drives the CLI's native close_task hook payload through its real
    adapter translation (which applies shared tool-field normalization), then
    through RuleEngine evaluation against the synced bundled rule. Regression
    coverage for the list-shaped MCP tool_output that silently disabled the
    rule condition before #20807.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "build_event"),
        [
            (
                "claude-content-block-list",
                lambda: _claude_close_event([{"type": "text", "text": _CLOSE_PAYLOAD_TEXT}]),
            ),
            ("claude-dict-envelope", lambda: _claude_close_event(dict(_MCP_RESULT_ENVELOPE))),
            ("codex-app-server-mcp-tool-call", _codex_app_server_close_event),
            ("codex-hooks-json", _codex_hooks_close_event),
            ("grok-dict-tool-result", lambda: _grok_close_event(dict(_MCP_RESULT_ENVELOPE))),
            ("grok-string-tool-result", lambda: _grok_close_event(_CLOSE_PAYLOAD_TEXT)),
            ("qwen-post-tool-use", _qwen_close_event),
            ("droid-post-tool-use", _droid_close_event),
        ],
    )
    async def test_native_close_payload_queues_compact_self(
        self,
        db: HubDatabase,
        label: str,
        build_event: Any,
    ) -> None:
        _sync_bundled(db)
        event = build_event()
        assert event is not None, f"{label}: adapter did not translate the payload"
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.data.get("mcp_server") == "gobby-tasks", label
        assert event.data.get("mcp_tool") == "close_task", label
        tool_output = event.data.get("tool_output")
        assert isinstance(tool_output, dict), (
            f"{label}: tool_output is {type(tool_output).__name__}"
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "claimed_tasks": {
                    "task-a": {"task_type": "task"},
                    "task-b": {"task_type": "task"},
                }
            },
        )

        compact_calls = [
            call
            for call in response.metadata.get("mcp_calls", [])
            if call["server"] == "gobby-sessions" and call["tool"] == "compact_self"
        ]
        assert len(compact_calls) == 1, label
        assert compact_calls[0]["arguments"] == {"rule_name": "auto-compact-after-task-close"}
