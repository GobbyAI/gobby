"""Tests for the bounded post-close Gobby session-feedback survey."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

SESSION_ID = "b62f0102-3ee3-4bb4-9f9d-57a523974726"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")


def _event(event_type: HookEventType, data: dict[str, object] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


class TestSessionFeedbackRules:
    def test_bundled_feedback_rule_is_bounded_and_post_close(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("review-gobby-session-feedback-on-stop")

        assert row is not None
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
        _sync_bundled(db)
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
    async def test_feedback_review_allows_without_completed_work(self, db: HubDatabase) -> None:
        _sync_bundled(db)
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
        "variables",
        [
            {"task_claimed": True, "session_task": "#42"},
            {},
        ],
    )
    async def test_compact_review_blocks_once_for_owned_or_taskless_sessions(
        self, db: HubDatabase, variables: dict[str, object]
    ) -> None:
        _sync_bundled(db)
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
        _sync_bundled(db)
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
    async def test_compact_session_start_resets_feedback_epoch(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        with db.transaction() as conn:
            conn.execute("UPDATE rule_definitions SET enabled = FALSE")
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
                ("reset-gobby-session-feedback-on-context-reset",),
            )
        variables: dict[str, object] = {"_gobby_feedback_epoch_reviewed": True}

        result = await RuleEngine(db).evaluate(
            _event(HookEventType.SESSION_START, {"source": "compact"}),
            SESSION_ID,
            variables,
        )

        assert result.decision == "allow"
        assert variables["_gobby_feedback_epoch_reviewed"] is False
