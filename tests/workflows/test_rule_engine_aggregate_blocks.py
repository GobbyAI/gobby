"""Tests for declarative rule block aggregation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.skills.formatting import SKILL_BLOCK_ATOMICITY_NOTICE, skill_fetch_directive
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.blocked_tool_recovery import format_aggregated_block_reason
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


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


def _make_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Edit", "tool_input": {"file_path": "example.py"}},
    )


def _insert_rule(
    manager: RuleDefinitionManager,
    name: str,
    effects: list[RuleEffect],
    *,
    priority: int,
    when: str | None = None,
) -> None:
    body = RuleDefinitionBody(
        event=RuleTriggerEvent.BEFORE_TOOL,
        when=when,
        effects=effects,
    )
    manager.create(
        name=name,
        definition_json=json.dumps(body.model_dump(mode="json")),
        priority=priority,
        enabled=True,
    )


class TestAggregateBlocks:
    @pytest.mark.asyncio
    async def test_matching_blocks_aggregate_in_priority_order(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "second-gate",
            [RuleEffect(type="block", reason="Second gate")],
            priority=20,
        )
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
        )

        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            "1. [first-gate] First gate\n"
            "2. [second-gate] Second gate"
        )

    async def test_duplicate_skill_directives_are_rendered_once(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        python_directive = (
            "Load and fully read the skill in its own outer tool result: "
            'call_tool("gobby-skills", "get_skill", {"name":"python"}). Then continue.'
        )
        restraint_directive = (
            "Load and fully read the skill in its own outer tool result: "
            'call_tool("gobby-skills", "get_skill", {"name":"restraint"}). Then continue.'
        )
        _insert_rule(
            manager,
            "require-claimed-task-required-skills",
            [RuleEffect(type="block", reason=python_directive)],
            priority=10,
        )
        _insert_rule(
            manager,
            "require-python-skill",
            [RuleEffect(type="block", reason=python_directive)],
            priority=20,
        )
        _insert_rule(
            manager,
            "require-restraint-skill",
            [RuleEffect(type="block", reason=restraint_directive)],
            priority=30,
        )

        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})

        assert response.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            f"{SKILL_BLOCK_ATOMICITY_NOTICE}\n"
            "Multiple gates blocked while retrying Edit.\n"
            f"1. [require-claimed-task-required-skills] {python_directive}\n"
            f"2. [require-restraint-skill] {restraint_directive}"
        )

    def test_aggregate_formatter_omits_placeholder_retry_target(self) -> None:
        assert format_aggregated_block_reason(
            [("first-gate", "First gate"), ("second-gate", "Second gate")],
            tool_name="-",
        ) == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "1. [first-gate] First gate\n"
            "2. [second-gate] Second gate"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["git commit", "git add . && git commit"])
    async def test_skill_block_notice_covers_entire_call_and_repeated_block(
        self, db: HubDatabase, manager: RuleDefinitionManager, command: str
    ) -> None:
        _insert_rule(
            manager,
            "require-code-review-skill",
            [
                RuleEffect(
                    type="block",
                    reason=skill_fetch_directive("code-review") + " Review staged changes.",
                )
            ],
            priority=10,
        )
        event = _make_event()
        event.data = {"tool_name": "Bash", "tool_input": {"command": command}}
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)
        for _ in range(2):
            response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
            assert response.decision == "block"
            assert response.reason is not None
            assert response.reason.count(SKILL_BLOCK_ATOMICITY_NOTICE) == 1
            assert response.reason.index(SKILL_BLOCK_ATOMICITY_NOTICE) < response.reason.index(
                "call_tool"
            )
            assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_nonblocking_skill_context_has_no_atomicity_notice(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        directive = skill_fetch_directive("python")
        _insert_rule(
            manager,
            "suggest-python",
            [RuleEffect(type="inject_context", template=directive)],
            priority=10,
        )
        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})
        assert response.decision != "block"
        assert response.context is not None
        assert directive in response.context
        assert SKILL_BLOCK_ATOMICITY_NOTICE not in response.context

    @pytest.mark.asyncio
    async def test_single_block_output_is_unchanged(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "solo-gate",
            [RuleEffect(type="block", reason="Only gate")],
            priority=10,
        )

        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})

        assert response.reason == "Rule enforced by Gobby: [solo-gate]\nOnly gate"

    @pytest.mark.asyncio
    async def test_aggregate_blocks_false_restores_first_block_behavior(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
        )
        _insert_rule(
            manager,
            "second-gate",
            [
                RuleEffect(type="set_variable", variable="second_ran", value=True),
                RuleEffect(type="block", reason="Second gate"),
            ],
            priority=20,
        )
        variables: dict[str, Any] = {}

        engine = RuleEngine(db, config_runtime=_runtime_with({"rules.aggregate_blocks": False}))
        response = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert response.reason == "Rule enforced by Gobby: [first-gate]\nFirst gate"
        assert "second_ran" not in variables

    @pytest.mark.asyncio
    async def test_first_blocker_side_effects_run_and_lookahead_side_effects_do_not(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [
                RuleEffect(type="set_variable", variable="first_ran", value=True),
                RuleEffect(type="block", reason="First gate"),
            ],
            priority=10,
        )
        _insert_rule(
            manager,
            "second-gate",
            [
                RuleEffect(type="set_variable", variable="second_ran", value=True),
                RuleEffect(type="mcp_call", server="gobby-memory", tool="create_memory"),
                RuleEffect(type="block", reason="Second gate"),
            ],
            priority=20,
        )
        variables: dict[str, Any] = {}

        response = await RuleEngine(db).evaluate(
            _make_event(), session_id=SESSION_ID, variables=variables
        )

        assert variables["first_ran"] is True
        assert "second_ran" not in variables
        assert "mcp_calls" not in response.metadata
        assert response.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            "1. [first-gate] First gate\n"
            "2. [second-gate] Second gate"
        )

    @pytest.mark.asyncio
    async def test_lookahead_suppression_is_logged_and_recorded(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _insert_rule(
            manager,
            "primary-gate",
            [RuleEffect(type="block", reason="Primary gate")],
            priority=10,
        )
        _insert_rule(
            manager,
            "lookahead-gate",
            [
                RuleEffect(type="set_variable", variable="lookahead_ran", value=True),
                RuleEffect(type="inject_context", template="Suppressed context"),
                RuleEffect(type="block", reason="Lookahead gate"),
            ],
            priority=20,
        )
        metrics = MetricsEventStore(db)

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.engine.evaluation"):
            await RuleEngine(db, metrics_event_store=metrics).evaluate(
                _make_event(), session_id=SESSION_ID, variables={}
            )

        suppression_messages = [
            record.getMessage()
            for record in caplog.records
            if "Suppressed non-block rule effects" in record.getMessage()
        ]
        assert suppression_messages == [
            "Suppressed non-block rule effects during block-gate lookahead: "
            "rule=lookahead-gate event=before_tool "
            "effect_types=set_variable,inject_context"
        ]

        records = {
            record["name"]: record for record in metrics.query_events(event_type="rule_eval")
        }
        assert records["primary-gate"]["metadata_json"] is None
        assert records["lookahead-gate"]["result"] == "block"
        assert json.loads(records["lookahead-gate"]["metadata_json"]) == {
            "evaluation_mode": "lookahead"
        }

    @pytest.mark.asyncio
    async def test_aggregate_acknowledges_first_and_lookahead_block_gates(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "mandatory-gate",
            [RuleEffect(type="block", reason="Load mandatory skill")],
            priority=10,
            when="not variables.get('mandatory_skill_loaded')",
        )
        _insert_rule(
            manager,
            "context7-gate",
            [
                RuleEffect(
                    type="block",
                    reason="Optional context7 nudge",
                    acknowledge_variable="nudge_fired",
                )
            ],
            priority=20,
            when="not variables.get('nudge_fired')",
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        variables["mandatory_skill_loaded"] = True
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert first.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            "1. [mandatory-gate] Load mandatory skill\n"
            "2. [context7-gate] Optional context7 nudge"
        )
        assert variables["nudge_fired"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_single_acknowledged_block_only_fires_once(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "context7-gate",
            [
                RuleEffect(
                    type="block",
                    reason="Optional context7 nudge",
                    acknowledge_variable="nudge_fired",
                )
            ],
            priority=10,
            when="not variables.get('nudge_fired')",
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert first.reason == "Rule enforced by Gobby: [context7-gate]\nOptional context7 nudge"
        assert variables["nudge_fired"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_repeated_identical_aggregate_uses_verbose_once(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
        )
        _insert_rule(
            manager,
            "second-gate",
            [RuleEffect(type="block", reason="Second gate")],
            priority=20,
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert "1. [first-gate] First gate" in (first.reason or "")
        assert second.reason is not None
        assert second.reason.startswith("Rule enforced by Gobby: [aggregated:2-gates]")
        assert "full reason shown earlier" in second.reason

    @pytest.mark.asyncio
    async def test_reduced_remaining_gate_set_renders_fully(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
            when="not variables.get('skip_first', False)",
        )
        _insert_rule(
            manager,
            "second-gate",
            [RuleEffect(type="block", reason="Second gate")],
            priority=20,
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        variables["skip_first"] = True
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert "aggregated:2-gates" in (first.reason or "")
        assert second.reason == "Rule enforced by Gobby: [second-gate]\nSecond gate"
