"""Tests for bundled Gobby-experience survey capture gates."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.session_feedback_survey import (
    SURVEY_ACTIVE_VARIABLE,
    SURVEY_CONFIG_KEY,
    inject_survey_active,
    survey_is_active,
)
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

SESSION_ID = "b62f0102-3ee3-4bb4-9f9d-57a523974726"
PROJECT_ID = "c1a6d9e2-4b8f-4f0e-9a3d-2f7c6b1e8d05"
INBOX_PATH = "docs/research/gobby-feedback/inbox"
SURVEY_RULES = (
    "reset-gobby-session-feedback-on-context-reset",
    "mark-gobby-session-feedback-submitted",
    "review-gobby-session-feedback-before-handoff",
    "review-gobby-session-feedback-on-stop",
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _project(name: str = "gobby") -> dict[str, str]:
    return {"name": name, "id": PROJECT_ID, "path": "/tmp"}


def _runtime_with(values: dict[str, object]) -> Any:
    config = DaemonConfig()
    return cast(
        Any,
        SimpleNamespace(
            snapshot=ConfigSnapshot(
                revision=1,
                desired=config,
                active=config,
                row_revisions={},
                pending_restart_keys=frozenset(),
                failed_live_keys={},
                desired_values=values,
                active_values=values,
            )
        ),
    )


def _sync_bundled(db: HubDatabase) -> None:
    result = sync_bundled_rules(db, get_bundled_rules_path())
    assert result["errors"] == []


def _enable_rules(db: HubDatabase, *names: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        if names:
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = ANY(%s)",
                (list(names),),
            )


def _event(
    event_type: HookEventType,
    data: dict[str, object] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data or {},
        project_id=PROJECT_ID,
    )


def _close_task_event(*, success: bool = True, closed: bool = True) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-tasks",
            "mcp_tool": "close_task",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#42"},
            },
            "tool_output": {"success": success, "closed": closed},
        },
    )


def _offloaded_close_task_event(*, success: bool = True, closed: bool = True) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-tasks",
            "mcp_tool": "close_task",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#42"},
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


def _session_start_event(source: str, *, pending_context_reset: bool = False) -> HookEvent:
    return _event(
        HookEventType.SESSION_START,
        {"source": source, "_pending_context_reset": pending_context_reset},
    )


def _sessions_tool_event(tool_name: str, *, after: bool = False) -> HookEvent:
    data: dict[str, object] = {
        "tool_name": "mcp__gobby__call_tool",
        "mcp_server": "gobby-sessions",
        "mcp_tool": tool_name,
        "tool_input": {
            "server_name": "gobby-sessions",
            "tool_name": tool_name,
            "arguments": {},
        },
    }
    if after:
        data["tool_output"] = {"success": True}
        return _event(HookEventType.AFTER_TOOL, data)
    return _event(HookEventType.BEFORE_TOOL, data)


def _engine(
    db: HubDatabase,
    *,
    survey: str = "gobby",
) -> RuleEngine:
    return RuleEngine(
        db,
        config_runtime=_runtime_with({SURVEY_CONFIG_KEY: survey}),
    )


class TestSurveyIsActive:
    def test_supported_scopes_and_unknown_values(self) -> None:
        assert survey_is_active("all", "game-goblins") is True
        assert survey_is_active("gobby", "gobby") is True
        assert survey_is_active("gobby", "game-goblins") is False
        assert survey_is_active("off", "gobby") is False
        assert survey_is_active("unexpected", "gobby") is False

    def test_blank_scope_defaults_to_gobby(self) -> None:
        assert survey_is_active("", "gobby") is True
        assert survey_is_active("   ", "gobby") is True
        assert survey_is_active("", "game-goblins") is False
        assert survey_is_active(" ALL ", "anywhere") is True

    def test_inject_reads_project_name_and_config(self) -> None:
        variables: dict[str, Any] = {"project": _project("game-goblins")}
        inject_survey_active(variables, {})
        assert variables[SURVEY_ACTIVE_VARIABLE] is False
        inject_survey_active(variables, {SURVEY_CONFIG_KEY: "gobby"})
        assert variables[SURVEY_ACTIVE_VARIABLE] is False
        inject_survey_active(variables, {SURVEY_CONFIG_KEY: "all"})
        assert variables[SURVEY_ACTIVE_VARIABLE] is True

        variables = {"project": _project("gobby")}
        inject_survey_active(variables, {})
        assert variables[SURVEY_ACTIVE_VARIABLE] is True


class TestSessionFeedbackRules:
    def test_bundled_rules_are_product_gates(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        for name in SURVEY_RULES:
            row = manager.get_by_name(name)
            assert row is not None, name
            assert row.project_id is None
            assert "gobby" in (row.tags or [])
            assert "session-feedback" in (row.tags or [])

        reset = manager.get_by_name("reset-gobby-session-feedback-on-context-reset")
        assert reset is not None
        assert reset.priority == 8
        reset_body = RuleDefinitionBody.model_validate(reset.definition_json)
        assert reset_body.event.value == "session_start"

        stop = manager.get_by_name("review-gobby-session-feedback-on-stop")
        assert stop is not None
        assert stop.priority == 3
        stop_body = RuleDefinitionBody.model_validate(stop.definition_json)
        stop_reason = stop_body.resolved_effects[0].reason or ""
        assert "gobby-sessions:feedback" in stop_reason
        assert "missing-affordance" in stop_reason
        assert "kind_other_label" in stop_reason
        assert INBOX_PATH not in stop_reason
        assert "#21128" not in stop_reason

        handoff = manager.get_by_name("review-gobby-session-feedback-before-handoff")
        assert handoff is not None
        assert handoff.priority == 2
        handoff_body = RuleDefinitionBody.model_validate(handoff.definition_json)
        handoff_reason = handoff_body.resolved_effects[0].reason or ""
        assert "gobby-sessions:feedback" in handoff_reason
        assert "missing-affordance" in handoff_reason
        assert "kind_other_label" in handoff_reason
        assert INBOX_PATH not in handoff_reason
        assert "set_handoff" in (handoff_body.when or "")
        assert "compact_self" not in (handoff_body.when or "")

        # Task closure is not a context boundary: closing N tasks in one epoch
        # must not re-arm the survey N times.
        assert manager.get_by_name("rearm-gobby-session-feedback-after-close") is None

    @pytest.mark.asyncio
    async def test_evaluate_injects_survey_active_from_config(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        _enable_rules(db)
        variables: dict[str, Any] = {"project": _project("gobby")}
        await _engine(db, survey="off").evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
        assert variables[SURVEY_ACTIVE_VARIABLE] is False

        variables = {"project": _project("gobby")}
        await _engine(db, survey="all").evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
        assert variables[SURVEY_ACTIVE_VARIABLE] is True

    @pytest.mark.asyncio
    async def test_stop_gate_blocks_once_when_completed_work_is_pending(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        _enable_rules(db, "review-gobby-session-feedback-on-stop")
        variables: dict[str, Any] = {
            "project": _project(),
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
        }
        engine = _engine(db)
        event = _event(HookEventType.STOP)

        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert first.decision == "block"
        assert "gobby-sessions:feedback" in (first.reason or "")
        assert INBOX_PATH not in (first.reason or "")
        assert variables["_gobby_feedback_epoch_reviewed"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_set_handoff_blocks_once_and_get_handoff_never_blocks(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        _enable_rules(db, "review-gobby-session-feedback-before-handoff")
        variables: dict[str, Any] = {
            "project": _project(),
            "task_claimed": True,
            "session_task": "#42",
        }
        engine = _engine(db)

        first = await engine.evaluate(_sessions_tool_event("set_handoff"), SESSION_ID, variables)
        retry = await engine.evaluate(_sessions_tool_event("set_handoff"), SESSION_ID, variables)
        get_handoff = await engine.evaluate(
            _sessions_tool_event("get_handoff"), SESSION_ID, variables
        )

        assert first.decision == "block"
        assert "gobby-sessions:feedback" in (first.reason or "")
        assert INBOX_PATH not in (first.reason or "")
        assert variables["_gobby_feedback_epoch_reviewed"] is True
        assert retry.decision == "allow"
        assert get_handoff.decision == "allow"

    @pytest.mark.asyncio
    async def test_set_handoff_skips_unclaimed_session_task(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        _enable_rules(db, "review-gobby-session-feedback-before-handoff")
        variables: dict[str, Any] = {
            "project": _project(),
            "task_claimed": False,
            "session_task": "#42",
        }

        result = await _engine(db).evaluate(
            _sessions_tool_event("set_handoff"), SESSION_ID, variables
        )

        assert result.decision == "allow"
        assert "_gobby_feedback_epoch_reviewed" not in variables

    @pytest.mark.asyncio
    async def test_off_suppresses_stop_and_set_handoff_gates(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        _enable_rules(
            db,
            "review-gobby-session-feedback-on-stop",
            "review-gobby-session-feedback-before-handoff",
        )
        variables: dict[str, Any] = {
            "project": _project(),
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "task_claimed": True,
        }
        engine = _engine(db, survey="off")

        stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
        handoff = await engine.evaluate(_sessions_tool_event("set_handoff"), SESSION_ID, variables)

        assert stop.decision == "allow"
        assert handoff.decision == "allow"
        assert variables[SURVEY_ACTIVE_VARIABLE] is False

    @pytest.mark.asyncio
    async def test_gobby_scope_skips_other_project_names(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        _enable_rules(
            db,
            "review-gobby-session-feedback-on-stop",
            "review-gobby-session-feedback-before-handoff",
        )
        engine = _engine(db, survey="gobby")
        foreign: dict[str, Any] = {
            "project": _project("game-goblins"),
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "task_claimed": True,
        }
        owner: dict[str, Any] = {
            "project": _project("gobby"),
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "task_claimed": True,
        }

        foreign_stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, foreign)
        foreign_handoff = await engine.evaluate(
            _sessions_tool_event("set_handoff"), SESSION_ID, foreign
        )
        owner_stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, owner)

        assert foreign_stop.decision == "allow"
        assert foreign_handoff.decision == "allow"
        assert owner_stop.decision == "block"
        assert owner[SURVEY_ACTIVE_VARIABLE] is True

    @pytest.mark.asyncio
    async def test_all_scope_gates_other_project_names(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        _enable_rules(
            db,
            "review-gobby-session-feedback-on-stop",
            "review-gobby-session-feedback-before-handoff",
        )
        engine = _engine(db, survey="all")
        stop_variables: dict[str, Any] = {
            "project": _project("game-goblins"),
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
        }
        handoff_variables: dict[str, Any] = {
            "project": _project("game-goblins"),
            "task_claimed": True,
        }

        stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, stop_variables)
        handoff = await engine.evaluate(
            _sessions_tool_event("set_handoff"), SESSION_ID, handoff_variables
        )

        assert stop.decision == "block"
        assert handoff.decision == "block"
        assert stop_variables[SURVEY_ACTIVE_VARIABLE] is True
        assert handoff_variables[SURVEY_ACTIVE_VARIABLE] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("event", "expected_reviewed"),
        [
            (_session_start_event("compact"), False),
            (_session_start_event("clear"), False),
            (_session_start_event("resume", pending_context_reset=True), False),
            (_session_start_event("resume"), True),
            (_session_start_event("startup"), True),
        ],
        ids=("compact", "clear", "resume-after-reset", "plain-resume", "startup"),
    )
    async def test_context_reset_rearms_the_survey(
        self,
        db: HubDatabase,
        event: HookEvent,
        expected_reviewed: bool,
    ) -> None:
        _sync_bundled(db)
        _enable_rules(db, "reset-gobby-session-feedback-on-context-reset")
        variables: dict[str, Any] = {
            "project": _project(),
            "pending_context_reset": bool(event.data.get("_pending_context_reset")),
            "_gobby_feedback_epoch_reviewed": True,
        }

        await _engine(db).evaluate(event, SESSION_ID, variables)

        assert variables["_gobby_feedback_epoch_reviewed"] is expected_reviewed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event",
        [_close_task_event(), _offloaded_close_task_event()],
        ids=("direct-close", "offloaded-close"),
    )
    async def test_task_closure_never_rearms_the_survey(
        self, db: HubDatabase, event: HookEvent
    ) -> None:
        """One epoch is one survey, however many tasks the epoch closes."""
        _sync_bundled(db)
        _enable_rules(db, *SURVEY_RULES)
        variables: dict[str, Any] = {
            "project": _project(),
            "task_claimed": True,
            "_memory_pending_task_reviews": [{"task_ref": "#42"}],
            "_gobby_feedback_epoch_reviewed": True,
        }
        engine = _engine(db)

        await engine.evaluate(event, SESSION_ID, variables)
        stop = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
        handoff = await engine.evaluate(_sessions_tool_event("set_handoff"), SESSION_ID, variables)

        assert variables["_gobby_feedback_epoch_reviewed"] is True
        assert stop.decision == "allow"
        assert handoff.decision == "allow"

    @pytest.mark.asyncio
    async def test_successful_feedback_marks_epoch_reviewed(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        _enable_rules(db, "mark-gobby-session-feedback-submitted")
        variables: dict[str, Any] = {
            "project": _project(),
            "_gobby_feedback_epoch_reviewed": False,
        }

        await _engine(db).evaluate(
            _sessions_tool_event("feedback", after=True), SESSION_ID, variables
        )

        assert variables["_gobby_feedback_epoch_reviewed"] is True
