"""Blocked-tool accounting across parallel batches and concurrent agent contexts.

A parallel batch of sibling denials from one assistant response buys one
remediation attempt; separate responses, marked by the provider's tool-batch
boundary event, still escalate to the consecutive-tool-block guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.block_batching import BLOCK_SCOPES_VARIABLE
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL.
SESSION_ID = "11111111-1111-4111-8111-111111111111"

BLOCKED_RULE = "block-todowrite"
BLOCKED_REASON = "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


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


def _blocked_state() -> dict[str, Any]:
    """Recovery state a pre-execution gate leaves behind after blocking TodoWrite."""
    return {
        "_last_blocked_tool": "TodoWrite",
        "_last_blocked_rule_name": BLOCKED_RULE,
        "_last_blocked_reason": BLOCKED_REASON,
    }


async def _sibling_denial(
    engine: RuleEngine,
    variables: dict[str, Any],
    *,
    file_path: str,
    agent_id: str | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> str:
    """Replay one blocked sibling call and return the engine's decision."""
    data: dict[str, Any] = {"tool_name": "TodoWrite", "tool_input": {"file_path": file_path}}
    if agent_id is not None:
        data["agent_id"] = agent_id
    response = await engine.evaluate(_make_event(data=data, source=source), SESSION_ID, variables)
    return response.decision


async def _close_batch(
    engine: RuleEngine,
    variables: dict[str, Any],
    *,
    agent_id: str | None = None,
) -> None:
    """Deliver the provider's end-of-response tool-batch boundary."""
    data: dict[str, Any] = {"tool_calls": []}
    if agent_id is not None:
        data["agent_id"] = agent_id
    await engine.evaluate(
        _make_event(HookEventType.POST_TOOL_BATCH, data=data),
        SESSION_ID,
        variables,
    )


class TestParallelBatchCounting:
    """One assistant response's sibling denials are one remediation attempt."""

    @pytest.mark.asyncio
    async def test_five_parallel_denials_count_once(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            **_blocked_state(),
            "max_consecutive_blocked_tool_attempts": 5,
        }

        for index in range(5):
            decision = await _sibling_denial(engine, variables, file_path=f"a{index}.py")
            assert decision == "allow"
            # The gate re-blocks every sibling, restoring the recovery state.
            variables.update(_blocked_state())

        assert variables["consecutive_tool_blocks"] == 1

    @pytest.mark.asyncio
    async def test_batch_boundary_rearms_counting(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            **_blocked_state(),
            "max_consecutive_blocked_tool_attempts": 5,
        }

        for expected in (1, 2, 3):
            decision = await _sibling_denial(engine, variables, file_path="a.py")
            assert decision == "allow"
            assert variables["consecutive_tool_blocks"] == expected
            await _close_batch(engine, variables)
            variables.update(_blocked_state())

        response = await engine.evaluate(
            _make_event(data={"tool_name": "TodoWrite"}),
            SESSION_ID,
            variables,
        )
        assert response.decision == "block"
        assert response.reason is not None
        assert "5 times consecutively after repeated BEFORE_TOOL blocks" in response.reason

    @pytest.mark.asyncio
    async def test_batch_suppression_survives_a_full_batch_then_escalates(
        self, db: HubDatabase
    ) -> None:
        """Five-sibling batches still escalate once enough responses have passed."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            **_blocked_state(),
            "max_consecutive_blocked_tool_attempts": 5,
        }

        for _ in range(3):
            for index in range(5):
                assert (
                    await _sibling_denial(engine, variables, file_path=f"a{index}.py") == "allow"
                )
                variables.update(_blocked_state())
            await _close_batch(engine, variables)

        response = await engine.evaluate(
            _make_event(data={"tool_name": "TodoWrite"}),
            SESSION_ID,
            variables,
        )
        assert response.decision == "block"
        assert variables["consecutive_tool_blocks"] == 4

    @pytest.mark.asyncio
    async def test_cli_without_batch_boundary_counts_every_denial(self, db: HubDatabase) -> None:
        """Codex ships no tool-batch hook, so each denial remains its own attempt."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            **_blocked_state(),
            "max_consecutive_blocked_tool_attempts": 5,
        }

        for expected in (1, 2, 3):
            decision = await _sibling_denial(
                engine,
                variables,
                file_path="a.py",
                source=SessionSource.CODEX,
            )
            assert decision == "allow"
            assert variables["consecutive_tool_blocks"] == expected
            variables.update(_blocked_state())

        response = await engine.evaluate(
            _make_event(data={"tool_name": "TodoWrite"}, source=SessionSource.CODEX),
            SESSION_ID,
            variables,
        )
        assert response.decision == "block"


class TestAgentContextScoping:
    """Concurrent subagents sharing one session id keep separate block budgets."""

    @pytest.mark.asyncio
    async def test_one_subagents_blocks_do_not_charge_another(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "max_consecutive_blocked_tool_attempts": 3,
            BLOCK_SCOPES_VARIABLE: {
                "agent-a": _blocked_state(),
                "agent-b": _blocked_state(),
            },
        }

        assert (
            await _sibling_denial(engine, variables, file_path="a.py", agent_id="agent-a")
            == "allow"
        )
        await _close_batch(engine, variables, agent_id="agent-a")

        # agent-b has spent nothing, so its first denial must not escalate.
        assert (
            await _sibling_denial(engine, variables, file_path="b.py", agent_id="agent-b")
            == "allow"
        )

        scopes = variables[BLOCK_SCOPES_VARIABLE]
        assert scopes["agent-a"]["consecutive_tool_blocks"] == 1
        assert scopes["agent-b"]["consecutive_tool_blocks"] == 1
        assert "consecutive_tool_blocks" not in variables

    @pytest.mark.asyncio
    async def test_subagent_escalates_on_its_own_retries(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "max_consecutive_blocked_tool_attempts": 3,
            BLOCK_SCOPES_VARIABLE: {"agent-a": _blocked_state()},
        }

        assert (
            await _sibling_denial(engine, variables, file_path="a.py", agent_id="agent-a")
            == "allow"
        )
        await _close_batch(engine, variables, agent_id="agent-a")
        variables[BLOCK_SCOPES_VARIABLE]["agent-a"].update(_blocked_state())

        response = await engine.evaluate(
            _make_event(data={"tool_name": "TodoWrite", "agent_id": "agent-a"}),
            SESSION_ID,
            variables,
        )
        assert response.decision == "block"
        assert response.reason is not None
        assert BLOCKED_REASON in response.reason

    @pytest.mark.asyncio
    async def test_turn_start_clears_subagent_scopes(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            BLOCK_SCOPES_VARIABLE: {"agent-a": {**_blocked_state(), "consecutive_tool_blocks": 2}},
        }

        await engine.evaluate(
            _make_event(HookEventType.BEFORE_AGENT, data={"prompt": "next"}),
            SESSION_ID,
            variables,
        )

        assert variables[BLOCK_SCOPES_VARIABLE] == {}


class TestBlockReasonScoping:
    """Reason-shown state follows the agent context that received the reason."""

    _COLLAPSE_HINT = "(full reason shown earlier this turn — scroll up)."
    _FULL_REASON = "Load the python skill before editing Python sources."

    def _install_gate(self, manager: RuleDefinitionManager) -> None:
        manager.create(
            name="require-python-skill",
            definition_json=RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason=self._FULL_REASON)],
            ).model_dump(),
            priority=100,
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_subagent_first_block_is_not_collapsed(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        self._install_gate(manager)
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}

        main = await engine.evaluate(
            _make_event(data={"tool_name": "Edit"}),
            SESSION_ID,
            variables,
        )
        assert main.decision == "block"
        assert self._COLLAPSE_HINT not in (main.reason or "")

        subagent = await engine.evaluate(
            _make_event(data={"tool_name": "Edit", "agent_id": "agent-a"}),
            SESSION_ID,
            variables,
        )
        assert subagent.decision == "block"
        assert subagent.reason is not None
        assert self._COLLAPSE_HINT not in subagent.reason
        assert self._FULL_REASON in subagent.reason

    @pytest.mark.asyncio
    async def test_repeat_block_in_one_subagent_still_collapses(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        self._install_gate(manager)
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(data={"tool_name": "Edit", "agent_id": "agent-a"})

        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert self._COLLAPSE_HINT not in (first.reason or "")
        assert second.reason is not None
        assert self._COLLAPSE_HINT in second.reason
        shown = variables[BLOCK_SCOPES_VARIABLE]["agent-a"]["_block_reasons_shown"]
        assert len(shown) == 1
        assert variables.get("_block_reasons_shown") is None
