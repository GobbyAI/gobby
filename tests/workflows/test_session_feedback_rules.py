"""Tests for the bounded post-close Gobby session-feedback survey."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

SESSION_ID = "b62f0102-3ee3-4bb4-9f9d-57a523974726"
PROJECT_ID = "c1a6d9e2-4b8f-4f0e-9a3d-2f7c6b1e8d05"
OTHER_PROJECT_ID = "7e2b0f41-9c6d-4a15-8b3e-5d0a9f2c6e71"
# The feedback survey is a gobby-only experiment: a project rule, never bundled.
PROJECT_RULES_DIR = Path(__file__).resolve().parents[2] / ".gobby" / "workflows" / "rules"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_project_rules(db: HubDatabase) -> None:
    # Project rule rows reference ``projects``; register the owning project.
    db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "gobby-feedback-rules"),
    )
    result = sync_bundled_rules(
        db,
        [PROJECT_RULES_DIR],
        tag="user",
        project_id=PROJECT_ID,
        project_root=PROJECT_RULES_DIR,
    )
    assert result["errors"] == []


def _event(
    event_type: HookEventType,
    data: dict[str, object] | None = None,
    *,
    project_id: str = PROJECT_ID,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data or {},
        project_id=project_id,
    )


def _close_task_event(task_ref: str = "#42") -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-tasks",
            "mcp_tool": "close_task",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": task_ref},
            },
            "tool_output": {"success": True, "closed": True},
        },
    )


def _offloaded_close_task_event(
    task_ref: str = "#42",
    *,
    success: bool = True,
    closed: bool = True,
) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-tasks",
            "mcp_tool": "close_task",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": task_ref},
            },
            "tool_output": {
                "success": success,
                "result": {
                    "offloaded": True,
                    "result_id": "11111111-1111-1111-1111-111111111111",
                    "success": success,
                    "closed": closed,
                },
            },
        },
    )


def _enable_rules(db: HubDatabase, *names: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = ANY(%s)",
            (list(names),),
        )


class TestSessionFeedbackRules:
    def test_project_feedback_rules_are_bounded_and_closure_driven(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_project_rules(db)
        rearm = manager.get_by_name(
            "rearm-gobby-session-feedback-after-close", project_id=PROJECT_ID
        )
        row = manager.get_by_name("review-gobby-session-feedback-on-stop", project_id=PROJECT_ID)

        assert manager.get_by_name("reset-gobby-session-feedback-on-context-reset") is None
        assert rearm is not None
        assert rearm.project_id == PROJECT_ID
        assert "user" in (rearm.tags or [])
        assert "gobby" not in (rearm.tags or [])
        assert rearm.priority == 11
        rearm_body = RuleDefinitionBody.model_validate(rearm.definition_json)
        rearm_effects = rearm_body.resolved_effects
        assert rearm_body.event.value == "after_tool"
        assert "mcp_server" in (rearm_body.when or "")
        assert "mcp_tool" in (rearm_body.when or "")
        assert "tool_call_succeeded" in (rearm_body.when or "")
        assert "get('closed') is True" in (rearm_body.when or "")
        assert len(rearm_effects) == 1
        assert rearm_effects[0].variable == "_gobby_feedback_epoch_reviewed"
        assert rearm_effects[0].value is False

        assert manager.get_by_name("reset-gobby-session-feedback-on-context-reset") is None
        assert rearm is not None
        assert rearm.priority == 11
        rearm_body = RuleDefinitionBody.model_validate(rearm.definition_json)
        rearm_effects = rearm_body.resolved_effects
        assert rearm_body.event.value == "after_tool"
        assert "mcp_server" in (rearm_body.when or "")
        assert "mcp_tool" in (rearm_body.when or "")
        assert "tool_call_succeeded" in (rearm_body.when or "")
        assert "get('closed') is True" in (rearm_body.when or "")
        assert len(rearm_effects) == 1
        assert rearm_effects[0].variable == "_gobby_feedback_epoch_reviewed"
        assert rearm_effects[0].value is False

        assert row is not None
        assert row.project_id == PROJECT_ID
        assert row.priority == 3
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects

        assert body.event.value == "turn_end"
        assert "_memory_pending_task_reviews" in (body.when or "")
        assert "_gobby_feedback_epoch_reviewed" in (body.when or "")
        assert len(effects) == 1
        assert effects[0].type == "block"
        assert effects[0].acknowledge_variable == "_gobby_feedback_epoch_reviewed"
        reason = effects[0].reason or ""
        assert "at most 3 observations" in reason
        assert "Most sessions should produce no report" in reason
        assert "docs/research/gobby-feedback/inbox/" in reason
        assert "create a claimed task and fix it in" in reason
        assert "needs-decision" in reason
        assert "`Disposition` line" in reason
        assert "gobby-memory" in reason

    @pytest.mark.asyncio
    async def test_feedback_review_blocks_once_when_completed_work_is_pending(
        self, db: HubDatabase
    ) -> None:
        _sync_project_rules(db)
        with db.transaction() as conn:
            conn.execute("UPDATE rule_definitions SET enabled = FALSE")
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
                ("review-gobby-session-feedback-on-stop",),
            )
        variables: dict[str, object] = {
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
        }
        engine = RuleEngine(db)

        event = _event(HookEventType.STOP)
        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert first.decision == "block"
        assert "bounded Gobby session-feedback review" in (first.reason or "")
        assert variables["_gobby_feedback_epoch_reviewed"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_feedback_review_never_fires_for_another_project(self, db: HubDatabase) -> None:
        """The survey is a gobby project rule: other projects never see the gate."""
        _sync_project_rules(db)
        _enable_rules(db, "review-gobby-session-feedback-on-stop")
        variables: dict[str, object] = {
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
        }
        engine = RuleEngine(db)

        foreign = await engine.evaluate(
            _event(HookEventType.STOP, project_id=OTHER_PROJECT_ID), SESSION_ID, variables
        )
        assert foreign.decision == "allow"
        assert variables.get("_gobby_feedback_epoch_reviewed") is not True

        owner = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
        assert owner.decision == "block"

    @pytest.mark.asyncio
    async def test_feedback_review_allows_without_completed_work(self, db: HubDatabase) -> None:
        _sync_project_rules(db)
        with db.transaction() as conn:
            conn.execute("UPDATE rule_definitions SET enabled = FALSE")
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
                ("review-gobby-session-feedback-on-stop",),
            )
        variables: dict[str, object] = {}

        result = await RuleEngine(db).evaluate(_event(HookEventType.STOP), SESSION_ID, variables)

        assert result.decision == "allow"
        assert "_gobby_feedback_epoch_reviewed" not in variables

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("event", "expected_reviewed"),
        [
            (_offloaded_close_task_event(), False),
            (_offloaded_close_task_event(success=False), True),
            (_offloaded_close_task_event(closed=False), True),
        ],
        ids=("successful-close", "failed-call", "preview-only"),
    )
    async def test_offloaded_close_rearms_only_after_successful_closure(
        self,
        db: HubDatabase,
        event: HookEvent,
        expected_reviewed: bool,
    ) -> None:
        _sync_project_rules(db)
        _enable_rules(db, "rearm-gobby-session-feedback-after-close")
        variables: dict[str, object] = {"_gobby_feedback_epoch_reviewed": True}

        await RuleEngine(db).evaluate(event, SESSION_ID, variables)

        assert variables["_gobby_feedback_epoch_reviewed"] is expected_reviewed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "variables",
        [
            {"task_claimed": True, "session_task": "#42"},
            {},
        ],
    )
    async def test_compact_review_blocks_once_for_owned_or_taskless_sessions(
        self, db: HubDatabase, variables: dict[str, object]
    ) -> None:
        _sync_project_rules(db)
        with db.transaction() as conn:
            conn.execute("UPDATE rule_definitions SET enabled = FALSE")
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
                ("review-gobby-session-feedback-before-compact",),
            )
        engine = RuleEngine(db)
        event = _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "mcp_server": "gobby-sessions",
                "mcp_tool": "compact_self",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "compact_self",
                    "arguments": {},
                },
            },
        )

        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert first.decision == "block"
        assert "before compacting" in (first.reason or "").lower()
        assert variables["_gobby_feedback_epoch_reviewed"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_compact_review_skips_unclaimed_session_task(self, db: HubDatabase) -> None:
        _sync_project_rules(db)
        with db.transaction() as conn:
            conn.execute("UPDATE rule_definitions SET enabled = FALSE")
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
                ("review-gobby-session-feedback-before-compact",),
            )
        variables: dict[str, object] = {
            "task_claimed": False,
            "session_task": "#42",
        }
        event = _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "mcp_server": "gobby-sessions",
                "mcp_tool": "compact_self",
            },
        )

        result = await RuleEngine(db).evaluate(event, SESSION_ID, variables)

        assert result.decision == "allow"
        assert "_gobby_feedback_epoch_reviewed" not in variables

    @pytest.mark.asyncio
    async def test_feedback_acknowledgement_survives_compaction_without_a_new_close(
        self, db: HubDatabase
    ) -> None:
        _sync_project_rules(db)
        _enable_rules(
            db,
            "rearm-gobby-session-feedback-after-close",
            "review-gobby-session-feedback-before-compact",
            "review-gobby-session-feedback-on-stop",
        )
        variables: dict[str, object] = {
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "_gobby_feedback_epoch_reviewed": True,
            "task_claimed": True,
            "session_task": "#21080",
        }
        engine = RuleEngine(db)

        resumed = await engine.evaluate(
            _event(HookEventType.SESSION_START, {"source": "compact"}),
            SESSION_ID,
            variables,
        )
        compact = await engine.evaluate(
            _event(
                HookEventType.BEFORE_TOOL,
                {"mcp_server": "gobby-sessions", "mcp_tool": "compact_self"},
            ),
            SESSION_ID,
            variables,
        )
        stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)

        assert resumed.decision == "allow"
        assert compact.decision == "allow"
        assert stop.decision == "allow"
        assert variables["_gobby_feedback_epoch_reviewed"] is True

    @pytest.mark.asyncio
    async def test_successful_closure_batch_rearms_feedback_once_after_compaction(
        self, db: HubDatabase
    ) -> None:
        _sync_project_rules(db)
        _enable_rules(
            db,
            "rearm-gobby-session-feedback-after-close",
            "review-gobby-session-feedback-on-stop",
        )
        variables: dict[str, object] = {
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "_gobby_feedback_epoch_reviewed": True,
        }
        engine = RuleEngine(db)

        await engine.evaluate(
            _event(HookEventType.SESSION_START, {"source": "compact"}),
            SESSION_ID,
            variables,
        )
        await engine.evaluate(_offloaded_close_task_event("#43"), SESSION_ID, variables)
        await engine.evaluate(_close_task_event("#44"), SESSION_ID, variables)
        first = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
        second = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)

        assert first.decision == "block"
        assert "bounded Gobby session-feedback review" in (first.reason or "")
        assert variables["_gobby_feedback_epoch_reviewed"] is True
        assert second.decision == "allow"
