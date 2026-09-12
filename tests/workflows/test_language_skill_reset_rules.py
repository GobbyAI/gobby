"""Regression tests for per-file language gates after context reset."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
LANGUAGE_CASES = (
    ("/project/src/app.py", "python"),
    ("/project/config/app.json", "json"),
    ("/project/config/app.yaml", "yaml"),
)


def _sync_language_rules(db: HubDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    enabled = ["reset-skill-injection", *(f"require-{skill}-skill" for _, skill in LANGUAGE_CASES)]
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = ANY(%s)",
            (enabled,),
        )


def _event(event_type: HookEventType, data: dict[str, object]) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("file_path", "expected_skill"), LANGUAGE_CASES)
async def test_reset_reactivates_only_the_matching_language_gate(
    temp_db: HubDatabase,
    file_path: str,
    expected_skill: str,
) -> None:
    _sync_language_rules(temp_db)
    engine = RuleEngine(temp_db)
    variables: dict[str, object] = {
        "loaded_skills": [skill for _, skill in LANGUAGE_CASES],
    }

    await engine.evaluate(
        _event(HookEventType.SESSION_START, {"source": "compact"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    assert variables["loaded_skills"] == []

    response = await engine.evaluate(
        _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_file_path": file_path,
                "tool_input": {"file_path": file_path},
            },
        ),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert response.decision == "block"
    assert expected_skill in (response.reason or "")
    assert skill_fetch_directive(expected_skill) in (response.reason or "")
    for _, other_skill in LANGUAGE_CASES:
        if other_skill != expected_skill:
            assert skill_fetch_directive(other_skill) not in (response.reason or "")


@pytest.mark.parametrize("source,pending", [("compact", False), ("clear", False), ("resume", True)])
async def test_reference_contract_1_2_3(temp_db: HubDatabase, source: str, pending: bool) -> None:
    _sync_language_rules(temp_db)
    variables: dict[str, object] = {
        "loaded_skills": ["brevity", "gobby"],
        "loaded_skill_references": ["gobby:references/tasks/closing.md"],
        "brevity_level": "max",
        "pending_context_reset": pending,
    }
    await RuleEngine(temp_db).evaluate(
        _event(HookEventType.SESSION_START, {"source": source}),
        session_id=SESSION_ID,
        variables=variables,
    )
    assert variables["loaded_skills"] == []
    assert variables["loaded_skill_references"] == []
    assert variables["brevity_level"] == "max"
