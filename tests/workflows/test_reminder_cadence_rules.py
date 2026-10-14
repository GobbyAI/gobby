"""Per-turn reminder cadence: once per context epoch plus every 5 turns.

The brevity/restraint one-liners used to inject
on every turn. They now key off ``turns_since_compact`` (observer-maintained,
reset to 0 on pre_compact) and a per-rule marker variable so a 10-turn session
sees at most two injections per reminder (#20448).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _sync_bundled(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")


def _turn_start_event(prompt: str = "keep going") -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": prompt},
    )


def _base_variables() -> dict[str, Any]:
    return {
        "loaded_skills": ["brevity", "restraint"],
        "brevity_level": "normal",
        "restraint_level": "normal",
    }


async def _run_turns(
    engine: RuleEngine,
    variables: dict[str, Any],
    turns: range,
) -> list[str]:
    """Evaluate one turn_start per turn number, mimicking the observer's counter."""
    contexts: list[str] = []
    for turn in turns:
        variables["turns_since_compact"] = turn
        result = await engine.evaluate(
            _turn_start_event(), session_id=SESSION_ID, variables=variables
        )
        contexts.append(result.context or "")
    return contexts


class TestReminderCadence:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("skill", ["brevity", "restraint"])
    async def test_pending_pull_keeps_opt_out_live(self, temp_db: HubDatabase, skill: str) -> None:
        _sync_bundled(temp_db)
        variables = {**_base_variables(), "handoff_pull_pending": {"attempt_id": "a" * 32}}
        response = await RuleEngine(temp_db).evaluate(
            _turn_start_event(f"stop {skill}"), SESSION_ID, variables
        )
        assert variables[f"{skill}_disabled"] is True
        assert f"{skill.title()} reminder" not in (response.context or "")

    @pytest.mark.asyncio
    async def test_pending_handoff_pull_suppresses_turn_start_reminders(
        self, temp_db: HubDatabase
    ) -> None:
        _sync_bundled(temp_db)
        engine = RuleEngine(temp_db)
        variables = {
            **_base_variables(),
            "parent_turn_seq": 0,
            "handoff_pull_pending": {"attempt_id": "a" * 32},
        }

        contexts = await _run_turns(engine, variables, range(1, 2))
        assert "Brevity reminder" not in contexts[0]
        assert "Restraint reminder" not in contexts[0]
        assert variables["turns_since_compact"] == 1
        assert variables["parent_turn_seq"] == 1
        assert "brevity_reminder_turn" not in variables
        assert "restraint_reminder_turn" not in variables

        variables.pop("handoff_pull_pending")
        resumed = await _run_turns(engine, variables, range(2, 3))
        assert "Brevity reminder" in resumed[0]
        assert "Restraint reminder" in resumed[0]

    @pytest.mark.asyncio
    async def test_ten_turn_session_caps_brevity_and_restraint_reminders(
        self, temp_db: HubDatabase
    ) -> None:
        _sync_bundled(temp_db)
        engine = RuleEngine(temp_db)
        variables = _base_variables()

        contexts = await _run_turns(engine, variables, range(1, 11))

        brevity_hits = [i for i, ctx in enumerate(contexts, start=1) if "Brevity reminder" in ctx]
        restraint_hits = [
            i for i, ctx in enumerate(contexts, start=1) if "Restraint reminder" in ctx
        ]
        assert brevity_hits == [1, 6]
        assert restraint_hits == [1, 6]

    @pytest.mark.asyncio
    async def test_compact_resets_markers_and_new_epoch_reinjects_once(
        self, temp_db: HubDatabase
    ) -> None:
        _sync_bundled(temp_db)
        engine = RuleEngine(temp_db)
        variables = _base_variables()

        first_epoch = await _run_turns(engine, variables, range(1, 4))
        assert "Brevity reminder" in first_epoch[0]
        assert variables["brevity_reminder_turn"] == 1

        compact_event = HookEvent(
            event_type=HookEventType.PRE_COMPACT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"trigger": "manual"},
        )
        await engine.evaluate(compact_event, session_id=SESSION_ID, variables=variables)
        assert variables["brevity_reminder_turn"] is None
        assert variables["restraint_reminder_turn"] is None

        second_epoch = await _run_turns(engine, variables, range(1, 4))
        brevity_hits = [
            i for i, ctx in enumerate(second_epoch, start=1) if "Brevity reminder" in ctx
        ]
        assert brevity_hits == [1]

    @pytest.mark.asyncio
    async def test_stored_zero_marker_does_not_remind_every_turn(
        self, temp_db: HubDatabase
    ) -> None:
        _sync_bundled(temp_db)
        engine = RuleEngine(temp_db)
        variables = _base_variables()
        variables["brevity_reminder_turn"] = 0
        variables["restraint_reminder_turn"] = 0

        contexts = await _run_turns(engine, variables, range(0, 5))

        joined = "\n".join(contexts)
        assert "Brevity reminder" not in joined
        assert "Restraint reminder" not in joined

    @pytest.mark.asyncio
    async def test_disabled_flags_still_suppress_reminders(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)
        engine = RuleEngine(temp_db)
        variables = _base_variables()
        variables["brevity_disabled"] = True
        variables["restraint_disabled"] = True

        contexts = await _run_turns(engine, variables, range(1, 4))

        joined = "\n".join(contexts)
        assert "Brevity reminder" not in joined
        assert "Restraint reminder" not in joined
