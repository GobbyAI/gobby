"""Tests for surface_memories delivery as a de-duplicated memory index."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.receipt_effects import apply_acknowledged_receipt, take_worker_staging
from gobby.memory.surface_format import LEAD_CHARS, format_memory_index
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

# Session/project id columns are native uuid in PostgreSQL; synthetic ids would
# fail with `invalid input syntax for type uuid`.
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
EXTERNAL_SESSION_ID = "11111111-1111-4111-8111-111111111112"
PLATFORM_SESSION_ID = "22222222-2222-4222-8222-222222222223"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"

LESSON_CONTENT = "Codex installs a matcherless native SessionEnd hook: ghook --gobby-owned"
LESSON_RATIONALE = "changing Codex hooks or agent termination"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    temp_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "memory-index-delivery"),
    )
    # session_variables carries a foreign key onto sessions, so the platform
    # session the index dedupes against has to exist before ids commit.
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, %s, 'test', %s) ON CONFLICT (id) DO NOTHING",
        (PLATFORM_SESSION_ID, EXTERNAL_SESSION_ID, MACHINE_ID, PROJECT_ID),
    )
    return temp_db


@pytest.fixture(autouse=True)
def _reset_worker_staging() -> Iterator[None]:
    take_worker_staging()
    yield
    take_worker_staging()


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    return RuleEngine(db)


def _vars(db: HubDatabase, session_id: str) -> dict[str, Any]:
    return SessionVariableManager(db).get_variables(session_id)


def _event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Task"},
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )


def _hit(memory_id: str, **overrides: Any) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "id": memory_id,
        "type": "pattern",
        "search_via": "semantic|keyword|graph",
        "updated_at": "2026-09-11T10:00:00+00:00",
        "content": LESSON_CONTENT,
        "rationale": LESSON_RATIONALE,
    }
    hit.update(overrides)
    return hit


def _surface(
    engine: RuleEngine,
    hits: list[dict[str, Any]],
    trigger: str = "spawn_agent",
) -> tuple[bool, str | None, list[str]]:
    return engine._format_memory_backed_result(
        server="gobby-memory",
        tool="surface_memories",
        result={"trigger": trigger, "count": len(hits), "memories": hits},
        event=_event(),
        platform_session_id=PLATFORM_SESSION_ID,
        variables={},
    )


def test_index_line_format(engine: RuleEngine) -> None:
    handled, formatted, review_lesson_ids = _surface(
        engine,
        [_hit("002c13ae-4b1f-4d4a-9a1e-6f0d2a3b4c5d", similarity=0.7312)],
    )

    assert handled is True
    # The surface path stages its own ids; it delivers no review lessons.
    assert review_lesson_ids == []
    assert formatted is not None
    lines = formatted.splitlines()
    assert lines[0] == '<memory-index trigger="spawn_agent">'
    assert lines[1] == (
        "1. 002c13ae [pattern; semantic+keyword+graph; updated 2026-09-11] "
        f"{LESSON_CONTENT} | when: {LESSON_RATIONALE}"
    )
    assert lines[-2] == (
        'Fetch full text before acting on a match: gobby-memory:get_memory(memory_id="<id>")'
    )
    assert lines[-1] == "</memory-index>"
    assert "0.7312" not in formatted


def test_index_truncates_only_after_a_whole_word() -> None:
    formatted = format_memory_index(
        "task",
        [_hit("002c13ae-4b1f-4d4a-9a1e-6f0d2a3b4c5d", content="complete " * 20)],
    )

    content = formatted.splitlines()[1].split("] ", 1)[1].split(" | when:", 1)[0]
    assert content == f"{' '.join(['complete'] * 17)}…"


@pytest.mark.parametrize(
    "content",
    [
        "x" * (LEAD_CHARS + 1),
        f"{'x' * LEAD_CHARS} trailing words",
    ],
)
def test_index_omits_an_overlong_first_word_instead_of_splitting_it(content: str) -> None:
    formatted = format_memory_index(
        "task",
        [_hit("002c13ae-4b1f-4d4a-9a1e-6f0d2a3b4c5d", content=content)],
    )

    rendered = formatted.splitlines()[1].split("] ", 1)[1].split(" | when:", 1)[0]
    assert rendered == "…"


def test_index_omits_the_when_clause_without_a_rationale(engine: RuleEngine) -> None:
    _handled, formatted, _ids = _surface(
        engine,
        [_hit("7f0c9d2e-4b1f-4d4a-9a1e-6f0d2a3b4c5d", rationale=None)],
    )

    assert formatted is not None
    assert formatted.splitlines()[1] == (
        f"1. 7f0c9d2e [pattern; semantic+keyword+graph; updated 2026-09-11] {LESSON_CONTENT}"
    )
    assert "when:" not in formatted


def test_index_dedupes_and_stages_ids(engine: RuleEngine, db: HubDatabase) -> None:
    first_id = "002c13ae-4b1f-4d4a-9a1e-6f0d2a3b4c5d"
    second_id = "7f0c9d2e-4b1f-4d4a-9a1e-6f0d2a3b4c5d"

    _handled, first, _ids = _surface(engine, [_hit(first_id)])
    assert first is not None
    assert "002c13ae" in first
    assert "injected_memory_ids" not in _vars(db, PLATFORM_SESSION_ID)

    _handled, second, _ids = _surface(engine, [_hit(first_id), _hit(second_id)])
    assert second is not None
    assert "002c13ae" not in second
    assert "7f0c9d2e" in second
    # The surviving hit is renumbered from the top of the delivered index.
    assert second.splitlines()[1].startswith("1. 7f0c9d2e ")

    staged = take_worker_staging()
    assert staged["append_set_variables"]["injected_memory_ids"] == [first_id, second_id]
    assert "injected_memory_ids" not in _vars(db, PLATFORM_SESSION_ID)

    apply_acknowledged_receipt(
        SimpleNamespace(
            receipt_id="memory-index-ack",
            session_id=PLATFORM_SESSION_ID,
            staged_payload=staged,
        ),
        variable_manager=SessionVariableManager(db),
    )
    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == [first_id, second_id]

    _handled, third, _ids = _surface(engine, [_hit(first_id), _hit(second_id)])
    assert third is None


def test_empty_index_injects_nothing(engine: RuleEngine) -> None:
    handled, formatted, _ids = _surface(engine, [])

    assert handled is True
    assert formatted is None
    assert take_worker_staging() == {}


def test_unregistered_memory_tool_is_not_routed_to_the_index(engine: RuleEngine) -> None:
    handled, formatted, _ids = engine._format_memory_backed_result(
        server="gobby-memory",
        tool="search_memories",
        result={"count": 1, "memories": [_hit("002c13ae-4b1f-4d4a-9a1e-6f0d2a3b4c5d")]},
        event=_event(),
        platform_session_id=PLATFORM_SESSION_ID,
        variables={},
    )

    assert handled is False
    assert formatted is None
