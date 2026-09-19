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
    "require-development-discipline-skill": "gobby:references/development/obligations.md",
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


def _read_event(file_path: str = "/project/src/app.py") -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Read",
            "canonical_tool_kind": "read",
            "canonical_file_path": file_path,
            "tool_input": {"file_path": file_path},
        },
    )


def _mcp_event(server_name: str, tool_name: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": server_name,
            "mcp_tool": tool_name,
            "tool_input": {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": {},
            },
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
        pytest.param(
            ["restraint"], "gobby:references/development/obligations.md", id="discipline-missing"
        ),
        pytest.param(
            ["gobby:references/development/obligations.md"],
            "restraint",
            id="restraint-missing",
        ),
    ],
)
async def test_each_guidance_skill_independently_gates_claimed_source_work(
    temp_db: HubDatabase,
    loaded_skills: list[str],
    blocked_skill: str,
) -> None:
    _sync_only_guidance_rules(temp_db)

    response = await RuleEngine(temp_db).evaluate(
        _write_event(),
        session_id=SESSION_ID,
        variables={
            "claimed_task_is_source_work": True,
            "loaded_skills": [item for item in loaded_skills if ":references/" not in item],
            "loaded_skill_references": [item for item in loaded_skills if ":references/" in item],
        },
    )

    assert response.decision == "block"
    assert skill_fetch_directive(blocked_skill) in (response.reason or "")


@pytest.mark.asyncio
async def test_guidance_gates_allow_loaded_skills_and_unclaimed_writes(
    temp_db: HubDatabase,
) -> None:
    _sync_only_guidance_rules(temp_db)
    engine = RuleEngine(temp_db)

    loaded = await engine.evaluate(
        _write_event(),
        session_id=SESSION_ID,
        variables={
            "loaded_skills": ["restraint"],
            "loaded_skill_references": [GUIDANCE_RULES["require-development-discipline-skill"]],
        },
    )
    markdown = await engine.evaluate(
        _write_event("/project/docs/notes.md"),
        session_id=SESSION_ID,
        variables={"loaded_skills": ["restraint"]},
    )

    assert loaded.decision == "allow"
    # Nothing claimed, so the discipline gate has no source work to govern.
    assert markdown.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_name", "tool_name"),
    [
        pytest.param("gobby-tasks", "create_task", id="create-task"),
        pytest.param("gobby-agents", "spawn_agent", id="spawn-agent"),
        pytest.param("gobby-agents", "dispatch_batch", id="dispatch-batch"),
    ],
)
async def test_root_graph_expansion_requires_restraint(
    temp_db: HubDatabase,
    server_name: str,
    tool_name: str,
) -> None:
    _sync_only_guidance_rules(temp_db)
    engine = RuleEngine(temp_db)
    event = _mcp_event(server_name, tool_name)

    blocked = await engine.evaluate(
        event,
        session_id=SESSION_ID,
        variables={
            "is_spawned_agent": False,
            "loaded_skill_references": ["gobby:references/development/obligations.md"],
        },
    )
    allowed = await engine.evaluate(
        event,
        session_id=SESSION_ID,
        variables={
            "is_spawned_agent": False,
            "loaded_skills": ["restraint"],
            "loaded_skill_references": [GUIDANCE_RULES["require-development-discipline-skill"]],
        },
    )

    assert blocked.decision == "block"
    assert skill_fetch_directive("restraint") in (blocked.reason or "")
    assert allowed.decision == "allow"


async def test_claiming_source_work_gates_the_first_tool_call_that_touches_the_checkout(
    temp_db: HubDatabase,
) -> None:
    _sync_only_guidance_rules(temp_db)
    engine = RuleEngine(temp_db)
    claimed = {"claimed_task_is_source_work": True, "loaded_skills": ["restraint"]}

    read = await engine.evaluate(_read_event(), session_id=SESSION_ID, variables=claimed)
    markdown = await engine.evaluate(
        _write_event("/project/docs/notes.md"), session_id=SESSION_ID, variables=claimed
    )

    obligations = GUIDANCE_RULES["require-development-discipline-skill"]
    assert read.decision == "block"
    assert skill_fetch_directive(obligations) in (read.reason or "")
    # No extension list any more: under a source-work claim the gate owns the docs
    # write too, because the claim is what says the obligations are owed.
    assert markdown.decision == "block"

    loaded = await engine.evaluate(
        _read_event(),
        session_id=SESSION_ID,
        variables={**claimed, "loaded_skill_references": [obligations]},
    )
    assert loaded.decision == "allow"


async def test_the_claim_gate_never_blocks_the_mcp_call_that_clears_it(
    temp_db: HubDatabase,
) -> None:
    _sync_only_guidance_rules(temp_db)
    engine = RuleEngine(temp_db)
    claimed = {"claimed_task_is_source_work": True, "loaded_skills": ["restraint"]}

    for server_name, tool_name in (
        ("gobby-skills", "get_skill_file"),
        ("gobby-skills", "get_skill"),
        ("gobby-tasks", "close_task"),
        ("gobby-memory", "search_memories"),
    ):
        response = await engine.evaluate(
            _mcp_event(server_name, tool_name), session_id=SESSION_ID, variables=claimed
        )
        assert response.decision == "allow", f"{server_name}:{tool_name} was blocked"


async def test_a_claim_outside_source_categories_never_reaches_the_gate(
    temp_db: HubDatabase,
) -> None:
    _sync_only_guidance_rules(temp_db)
    engine = RuleEngine(temp_db)
    docs_claim = {"claimed_task_is_source_work": False, "loaded_skills": ["restraint"]}

    read = await engine.evaluate(_read_event(), session_id=SESSION_ID, variables=docs_claim)
    source_write = await engine.evaluate(
        _write_event(), session_id=SESSION_ID, variables=docs_claim
    )

    # The category is the whole signal now: a docs or research claim owes nothing here
    # even if it does touch a .py file, and require-task-before-edit still governs the
    # write itself.
    assert read.decision == "allow"
    assert source_write.decision == "allow"
