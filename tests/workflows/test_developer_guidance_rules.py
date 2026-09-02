"""Tests for independent authoring-discipline source-write gates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
GUIDANCE_RULES = {
    "require-development-discipline-skill": "development-discipline",
    "require-restraint-skill": "restraint",
}


def _sync_only_guidance_rules(db: HubDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = ANY(%s)",
            (list(GUIDANCE_RULES),),
        )


def _write_event(file_path: str = "/project/src/app.py") -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Write",
            "canonical_tool_kind": "write",
            "canonical_file_path": file_path,
            "tool_input": {"file_path": file_path},
        },
    )


@pytest.mark.parametrize(("rule_name", "skill_name"), GUIDANCE_RULES.items())
def test_guidance_rule_structure(
    temp_db: HubDatabase,
    rule_name: str,
    skill_name: str,
) -> None:
    _sync_only_guidance_rules(temp_db)
    row = RuleDefinitionManager(temp_db).get_by_name(rule_name)
    assert row is not None
    body = RuleDefinitionBody.model_validate(row.definition_json)

    assert body.event.value == "before_tool"
    assert "canonical_tool_kind" in (body.when or "")
    assert f"skill_loaded('{skill_name}')" in (body.when or "")
    assert body.effects is not None
    assert body.effects[0].reason == f'{{{{ skill_fetch_directive("{skill_name}") }}}}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loaded_skills", "blocked_skill"),
    [
        pytest.param(["restraint"], "development-discipline", id="discipline-missing"),
        pytest.param(
            ["development-discipline"],
            "restraint",
            id="restraint-missing",
        ),
    ],
)
async def test_each_guidance_skill_independently_gates_source_writes(
    temp_db: HubDatabase,
    loaded_skills: list[str],
    blocked_skill: str,
) -> None:
    _sync_only_guidance_rules(temp_db)

    response = await RuleEngine(temp_db).evaluate(
        _write_event(),
        session_id=SESSION_ID,
        variables={"loaded_skills": loaded_skills},
    )

    assert response.decision == "block"
    assert skill_fetch_directive(blocked_skill) in (response.reason or "")


@pytest.mark.asyncio
async def test_guidance_gates_allow_loaded_and_non_source_writes(temp_db: HubDatabase) -> None:
    _sync_only_guidance_rules(temp_db)
    engine = RuleEngine(temp_db)

    loaded = await engine.evaluate(
        _write_event(),
        session_id=SESSION_ID,
        variables={"loaded_skills": list(GUIDANCE_RULES.values())},
    )
    markdown = await engine.evaluate(
        _write_event("/project/docs/notes.md"),
        session_id=SESSION_ID,
        variables={"loaded_skills": []},
    )

    assert loaded.decision == "allow"
    assert markdown.decision == "allow"
