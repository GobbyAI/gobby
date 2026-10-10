"""Bound prompt-submit memory surfacing independently of unrelated defaults."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.receipt_effects import worker_staging_scope
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.integration

LATENCY_BOUND_SECONDS = 1.0
MEMORY_DELAY_SECONDS = 0.025
UNRELATED_DEFAULT_ROWS = 300_000


@pytest.mark.asyncio
async def test_prompt_submit_memory_read_ignores_large_unrelated_defaults(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    """Exercise the shipped rule and bridge with real, warmed scoped SQL reads.

    Setup and the cache warmup are outside the measured hook. The synthetic memory
    collaborator performs the real session read before its controlled recall delay;
    neither the bridge nor the state manager is replaced or artificially delayed.
    """
    session_id = session_manager.register(
        external_id="memory-surface-latency",
        machine_id=get_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    ).id
    variables = SessionVariableManager(temp_db)
    defaults = SessionVariableDefaultManager(temp_db)
    defaults.create("recall_enabled", True, project_id=sample_project["id"])
    defaults.create(
        "unrelated_history",
        [
            {"labels": [index, index + 1], "nested": {"value": index}}
            for index in range(UNRELATED_DEFAULT_ROWS)
        ],
        project_id=sample_project["id"],
    )
    variables.merge_variables(
        session_id, {"recall_limit": 5, "unrelated_transcript": "x" * 2_000_000}
    )
    rule_path = (
        Path(__file__).resolve().parents[3]
        / "src/gobby/install/shared/workflows/rules/memory-lifecycle"
        / "surface-memories-on-turn-start.yaml"
    )
    rule = yaml.safe_load(rule_path.read_text())["rules"]["surface-memories-on-turn-start"]
    RuleDefinitionManager(temp_db).create(
        name="surface-memories-on-turn-start",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.TURN_START,
            when=rule["when"],
            effects=[RuleEffect.model_validate(effect) for effect in rule["effects"]],
        ).model_dump(),
        priority=rule["priority"],
        enabled=True,
    )
    calls: list[dict[str, Any]] = []
    origin_thread = threading.get_ident()

    async def memory_dispatcher(
        server: str, tool: str, arguments: dict[str, Any], event: HookEvent
    ) -> dict[str, Any]:
        assert (server, tool) == ("gobby-memory", "surface_memories")
        assert threading.get_ident() == origin_thread
        assert event.metadata["_native_hook_type"] == "UserPromptSubmit"
        read_started = time.perf_counter()
        selected = variables.get_variable_subset(session_id, ("recall_enabled", "recall_limit"))
        read_seconds = time.perf_counter() - read_started
        assert selected == {"recall_enabled": True, "recall_limit": 5}
        recall_complete: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        timer = asyncio.get_running_loop().call_later(
            MEMORY_DELAY_SECONDS, recall_complete.set_result, None
        )
        try:
            await recall_complete
        finally:
            timer.cancel()
        calls.append({"arguments": arguments, "read_seconds": read_seconds})
        return {"success": True, "result": {"count": 0, "memories": [], "trigger": "turn"}}

    engine = RuleEngine(temp_db, mcp_dispatcher=memory_dispatcher)
    # Warm the real revision-keyed defaults cache after all definition writes.
    assert variables.get_variable_subset(session_id, ("recall_enabled",)) == {
        "recall_enabled": True
    }
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "recall the latency decision"},
        metadata={"_platform_session_id": session_id, "_native_hook_type": "UserPromptSubmit"},
    )
    started = time.perf_counter()
    with worker_staging_scope():
        await engine.evaluate(
            event,
            session_id=session_id,
            variables={
                "project": {"id": sample_project["id"], "path": "/tmp"},
                "parent_turn_seq": 1,
                "servers_listed": True,
            },
        )
    elapsed = time.perf_counter() - started
    assert len(calls) == 1
    assert calls[0]["arguments"] == {"text": "recall the latency decision", "trigger": "turn"}
    print(
        json.dumps(
            {
                "hook_seconds": elapsed,
                "scoped_read_seconds": calls[0]["read_seconds"],
                "bound_seconds": LATENCY_BOUND_SECONDS,
                "memory_delay_seconds": MEMORY_DELAY_SECONDS,
                "unrelated_default_rows": UNRELATED_DEFAULT_ROWS,
                "unrelated_stored_bytes": 2_000_000,
            },
            sort_keys=True,
        )
    )
    assert elapsed < LATENCY_BOUND_SECONDS, (
        f"UserPromptSubmit memory surfacing took {elapsed:.3f}s; "
        f"bound is {LATENCY_BOUND_SECONDS:.3f}s"
    )
