"""Tests for task-scoped memory review tools."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_write import derive_memory_create_provenance
from gobby.storage.memories import MemoryType
from gobby.storage.tasks import TaskNotFoundError

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111110042"
PROJECT_ID = "11111111-1111-4111-8111-111111110001"
TASK_ID = "22222222-2222-4222-8222-222222220001"


def _task(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": TASK_ID,
        "project_id": PROJECT_ID,
        "title": "Implement layered memory guidance",
        "seq_num": 42,
        "closed_at": datetime(2026, 8, 25, tzinfo=UTC),
        "closed_in_session_id": SESSION_ID,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _resolve_task_reference(_db: Any, ref: str, _project_id: str) -> str:
    """Mirror the storage contract: ``#N`` resolves or raises ``TaskNotFoundError``."""
    if ref == "#42":
        return TASK_ID
    raise TaskNotFoundError(f"Task {ref} not found in project")


@pytest.fixture(autouse=True)
def _task_reference_resolution() -> Iterator[None]:
    with patch(
        "gobby.mcp_proxy.tools.memory_review.resolve_task_reference",
        side_effect=_resolve_task_reference,
    ):
        yield


def _registry(
    *,
    task: SimpleNamespace | None = None,
    candidates: list[SimpleNamespace] | None = None,
) -> tuple[Any, MagicMock, MagicMock, MagicMock]:
    memory_manager = MagicMock()
    memory_manager.search_memories = AsyncMock(return_value=candidates or [])

    def get_task(task_id: str, project_id: str | None = None) -> SimpleNamespace:
        # LocalTaskManager.get_task raises ValueError for ``#N`` refs without a
        # project and for unknown UUIDs; it never returns ``None``.
        if task is not None and task_id == TASK_ID:
            return task
        raise ValueError(f"Task {task_id} not found")

    task_manager = MagicMock()
    task_manager.get_task.side_effect = get_task
    session = SimpleNamespace(id=SESSION_ID, project_id=PROJECT_ID)
    session_manager = MagicMock()
    session_manager.get.side_effect = lambda session_id: (
        session if session_id == SESSION_ID else None
    )
    registry = create_memory_registry(
        lambda: memory_manager,
        task_manager=task_manager,
        session_manager=session_manager,
    )
    return registry, memory_manager, task_manager, session_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 1, 3])
async def test_review_returns_candidates_and_records_success(candidate_count: int) -> None:
    candidates = [
        SimpleNamespace(
            id=f"memory-{index}",
            content=f"Candidate {index}",
            rationale=f"Durable reason {index}",
            memory_type=MemoryType.FACT,
            tags=["architecture"],
            similarity=0.91 - (index * 0.1),
        )
        for index in range(candidate_count)
    ]
    registry, memory_manager, _task_manager, session_manager = _registry(
        task=_task(), candidates=candidates
    )
    state_manager = MagicMock()
    state_manager.get_variables.return_value = {}
    state_manager_cls = MagicMock(return_value=state_manager)

    with patch(
        "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
        state_manager_cls,
    ):
        result = await registry.call(
            "review_task_memories",
            {
                "task_id": "#42",
                "changes_summary": "Implemented and verified all three memory layers.",
                "session_id": SESSION_ID,
            },
        )

    assert result["success"] is True
    assert result["task_id"] == TASK_ID
    assert result["task_ref"] == "#42"
    assert result["source_task_id"] == TASK_ID
    assert result["candidate_count"] == candidate_count
    assert [candidate["id"] for candidate in result["candidates"]] == [
        f"memory-{index}" for index in range(candidate_count)
    ]
    search_kwargs = memory_manager.search_memories.await_args.kwargs
    assert search_kwargs["query"] == (
        "Implement layered memory guidance\n\nImplemented and verified all three memory layers."
    )
    assert "injected_memory_ids" not in search_kwargs
    assert search_kwargs["project_id"] == PROJECT_ID
    assert search_kwargs["include_global"] is True
    state_manager_cls.assert_called_once_with(session_manager.db)
    state_manager.upsert_bounded_list_variable.assert_called_once()
    assert result["pending_reviews_complete"] is False
    state_manager.set_variable.assert_not_called()


CLOSURE_ID = f"{TASK_ID}:2026-08-25T00:00:00+00:00"


def _state_manager(variables: dict[str, Any]) -> tuple[MagicMock, MagicMock]:
    state_manager = MagicMock()
    state_manager.get_variables.return_value = variables
    return state_manager, MagicMock(return_value=state_manager)


@pytest.mark.asyncio
async def test_reviewing_every_queued_closure_releases_the_stop_gate() -> None:
    registry = _registry(task=_task())[0]
    state_manager, state_manager_cls = _state_manager(
        {
            "_memory_pending_task_reviews": [{"closure_id": CLOSURE_ID, "task_ref": "#42"}],
            "_memory_task_review_records": [{"closure_id": CLOSURE_ID}],
        }
    )

    with patch("gobby.mcp_proxy.tools.memory_review.SessionVariableManager", state_manager_cls):
        result = await registry.call(
            "review_task_memories",
            {"task_id": "#42", "changes_summary": "Reviewed.", "session_id": SESSION_ID},
        )

    assert result["success"] is True
    assert result["pending_reviews_complete"] is True
    state_manager.upsert_bounded_list_variable.assert_called_once()
    state_manager.get_variables.assert_called_once_with(SESSION_ID)
    state_manager.set_variable.assert_called_once_with(
        SESSION_ID, "_memory_review_stop_delivered", True
    )


@pytest.mark.asyncio
async def test_partial_review_leaves_the_stop_gate_armed() -> None:
    registry = _registry(task=_task())[0]
    state_manager, state_manager_cls = _state_manager(
        {
            "_memory_pending_task_reviews": [
                {"closure_id": CLOSURE_ID, "task_ref": "#42"},
                {"closure_id": "other-task:2026-08-25T00:00:00+00:00", "task_ref": "#43"},
            ],
            "_memory_task_review_records": [{"closure_id": CLOSURE_ID}],
        }
    )

    with patch("gobby.mcp_proxy.tools.memory_review.SessionVariableManager", state_manager_cls):
        result = await registry.call(
            "review_task_memories",
            {"task_id": "#42", "changes_summary": "Reviewed.", "session_id": SESSION_ID},
        )

    assert result["success"] is True
    assert result["pending_reviews_complete"] is False
    state_manager.set_variable.assert_not_called()


@pytest.mark.asyncio
async def test_review_rejects_blank_summary_before_search() -> None:
    registry, memory_manager, _task_manager, _session_manager = _registry(task=_task())

    result = await registry.call(
        "review_task_memories",
        {"task_id": "#42", "changes_summary": "  ", "session_id": SESSION_ID},
    )

    assert result["error"] == "blank_changes_summary"
    memory_manager.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_rejects_missing_identity() -> None:
    registry, memory_manager, _task_manager, _session_manager = _registry(task=_task())

    result = await registry.call(
        "review_task_memories",
        {"task_id": "#42", "changes_summary": "Completed work.", "session_id": ""},
    )

    assert result["error"] == "missing_session_identity"
    memory_manager.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_resolves_session_reference_when_direct_lookup_misses() -> None:
    registry, memory_manager, _task_manager, session_manager = _registry(task=_task())

    with (
        patch(
            "gobby.mcp_proxy.tools.memory_review.resolve_session_reference",
            return_value=SESSION_ID,
        ) as resolver,
        patch(
            "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
            return_value=MagicMock(),
        ),
    ):
        result = await registry.call(
            "review_task_memories",
            {"task_id": "#42", "changes_summary": "Completed work.", "session_id": "1111"},
        )

    assert result["success"] is True
    resolver.assert_called_once_with(session_manager.db, "1111")
    memory_manager.search_memories.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_rejects_unresolvable_session() -> None:
    registry, memory_manager, _task_manager, _session_manager = _registry(task=_task())

    with patch(
        "gobby.mcp_proxy.tools.memory_review.resolve_session_reference",
        side_effect=ValueError("Session 'nope' not found"),
    ):
        result = await registry.call(
            "review_task_memories",
            {"task_id": "#42", "changes_summary": "Completed work.", "session_id": "nope"},
        )

    assert result["error"] == "missing_session_identity"
    memory_manager.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_rejects_unresolved_or_foreign_task() -> None:
    missing_registry, missing_memory, _task_manager, _session_manager = _registry(task=None)
    missing = await missing_registry.call(
        "review_task_memories",
        {"task_id": "#404", "changes_summary": "Completed work.", "session_id": SESSION_ID},
    )

    foreign_registry, foreign_memory, _task_manager, _session_manager = _registry(
        task=_task(closed_in_session_id="33333333-3333-4333-8333-333333330001")
    )
    foreign = await foreign_registry.call(
        "review_task_memories",
        {"task_id": "#42", "changes_summary": "Completed work.", "session_id": SESSION_ID},
    )

    assert missing["error"] == "task_not_found"
    assert foreign["error"] == "foreign_session_closure"
    missing_memory.search_memories.assert_not_awaited()
    foreign_memory.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_rejects_task_from_another_project() -> None:
    registry, memory_manager, _task_manager, _session_manager = _registry(
        task=_task(project_id="44444444-4444-4444-8444-444444440001")
    )

    result = await registry.call(
        "review_task_memories",
        {"task_id": TASK_ID, "changes_summary": "Completed work.", "session_id": SESSION_ID},
    )

    assert result["error"] == "task_not_found"
    memory_manager.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_failure_records_no_review_completion() -> None:
    registry, memory_manager, _task_manager, _session_manager = _registry(task=_task())
    memory_manager.search_memories.side_effect = RuntimeError("embedding service unavailable")
    state_manager_cls = MagicMock()

    with patch(
        "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
        state_manager_cls,
    ):
        result = await registry.call(
            "review_task_memories",
            {"task_id": "#42", "changes_summary": "Completed work.", "session_id": SESSION_ID},
        )

    assert result["error"] == "memory_search_failed"
    assert "embedding service unavailable" in result["message"]
    state_manager_cls.assert_not_called()


_LADDER_MEMORY = SimpleNamespace(
    id="memory-ladder",
    content=(
        "Current positive timeout defaults are workflow evaluation 90s < daemon adapter 105s "
        "< provider client 120s, enforced by DaemonConfig.validate_hook_timeout_order. "
        "Changing adapter_timeout or workflow.timeout requires a daemon restart."
    ),
    rationale="Serve when changing hook deadlines.",
    memory_type=MemoryType.FACT,
    tags=["hooks", "timeouts"],
    similarity=0.84,
)

_UNRELATED_MEMORY = SimpleNamespace(
    id="memory-unrelated",
    content=(
        "Gobby web MemoryTab search architecture after task #16818: non-empty queries call "
        "useMemory.searchMemories and render the server's searchResults."
    ),
    rationale="Serve when changing the web memory tab.",
    memory_type=MemoryType.FACT,
    tags=["web", "memory"],
    similarity=0.31,
)

# A long-form, multi-topic changes_summary of the shape #21394 produced: it names
# constants that appear verbatim in ``_LADDER_MEMORY`` but buries them among the
# repeated path and process boilerplate every summary carries.
_LONG_SUMMARY = (
    "Refit the daemon hook timeout ladder so every internal bound answers before ghook's "
    "transport window closes. Added HOOK_TRANSPORT_WINDOW_SECONDS to src/gobby/config/hooks.py "
    "and lowered the adapter_timeout default from 105.0 to 26.0. Lowered workflow.timeout via "
    "DEFAULT_WORKFLOW_TIMEOUT_SECONDS in src/gobby/config/tasks.py from 90.0 to 24.0. Extended "
    "validate_hook_timeout_order in src/gobby/config/app.py so it spans all five members. "
    "Bounded WorkflowEvaluationRuntime.run in src/gobby/workflows/evaluation_runtime.py so a "
    "wedged runtime releases its adapter thread instead of pinning an executor slot. Replaced "
    "the flat git timeout in src/gobby/workflows/git_utils.py and derived it from the shared "
    "BlockingEffectDeadline in src/gobby/workflows/hooks.py, with a floor so a spent budget "
    "never degenerates to a zero-second scan that reads as a clean tree. Regenerated the Rust "
    "runtime config contract asset. Added tests across eight files in tests/config, "
    "tests/workflows, tests/hooks, and tests/servers; three carried hardcoded values that no "
    "longer validate. Validation: focused pytest runs green, ruff format clean, ruff check "
    "clean, mypy src/ clean, test-types audit reports zero new entries against the baseline. "
) * 3


@pytest.mark.asyncio
async def test_long_summary_is_embedded_verbatim_not_yake_compressed() -> None:
    """#21402: the summary reaches the vector leg whole, so identifiers survive.

    ``search_memories`` runs YAKE over ``query`` whenever ``embed_text`` is absent.
    YAKE ranks by term repetition, so on a changes_summary it keeps the boilerplate
    and discards the identifiers a memory records. Passing ``embed_text`` is what
    skips that compression.
    """
    from gobby.search.keywords import extract_keywords

    assert len(_LONG_SUMMARY) > 2000

    registry, memory_manager, _task_manager, _session_manager = _registry(
        task=_task(title="Fit the hook timeout ladder under ghook's transport window"),
        candidates=[_LADDER_MEMORY],
    )
    state_manager = MagicMock()
    state_manager.get_variables.return_value = {}

    with patch(
        "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
        MagicMock(return_value=state_manager),
    ):
        result = await registry.call(
            "review_task_memories",
            {
                "task_id": "#42",
                "changes_summary": _LONG_SUMMARY,
                "session_id": SESSION_ID,
            },
        )

    assert result["success"] is True
    search_kwargs = memory_manager.search_memories.await_args.kwargs
    embedded = search_kwargs["embed_text"]

    # The vector leg receives the whole query, not a compressed stand-in.
    assert embedded == search_kwargs["query"]
    identifiers = ("adapter_timeout", "workflow.timeout", "validate_hook_timeout_order")
    for identifier in identifiers:
        assert identifier in embedded

    # Guard the reason: without embed_text the service would embed YAKE's output,
    # which drops every one of those identifiers even though each appears verbatim
    # in the summary and in the memory the change invalidates.
    compressed = extract_keywords(search_kwargs["query"])
    assert compressed is not None
    for identifier in identifiers:
        assert identifier in _LADDER_MEMORY.content
        assert identifier not in compressed


@pytest.mark.asyncio
async def test_review_surfaces_the_invalidated_memory_and_not_an_unrelated_one() -> None:
    """#21402: recall must improve without degrading into returning noise."""

    def _search(**kwargs: Any) -> list[SimpleNamespace]:
        # Stand in for the vector leg: a memory is a candidate when the text the
        # caller hands the embedder shares its distinguishing identifiers.
        embedded = kwargs["embed_text"] or ""
        hits = []
        if all(
            identifier in embedded
            for identifier in ("adapter_timeout", "workflow.timeout", "validate_hook_timeout_order")
        ):
            hits.append(_LADDER_MEMORY)
        if "MemoryTab" in embedded or "searchResults" in embedded:
            hits.append(_UNRELATED_MEMORY)
        return hits

    registry, memory_manager, _task_manager, _session_manager = _registry(
        task=_task(title="Fit the hook timeout ladder under ghook's transport window"),
    )
    memory_manager.search_memories = AsyncMock(side_effect=_search)
    state_manager = MagicMock()
    state_manager.get_variables.return_value = {}

    with patch(
        "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
        MagicMock(return_value=state_manager),
    ):
        result = await registry.call(
            "review_task_memories",
            {
                "task_id": "#42",
                "changes_summary": _LONG_SUMMARY,
                "session_id": SESSION_ID,
            },
        )

    returned = [candidate["id"] for candidate in result["candidates"]]
    assert "memory-ladder" in returned
    assert "memory-unrelated" not in returned


def test_closed_task_is_accepted_as_explicit_create_memory_source() -> None:
    db = MagicMock()
    with patch(
        "gobby.storage.tasks._id.resolve_task_reference",
        return_value=TASK_ID,
    ):
        source_task_id, created_by_agent = derive_memory_create_provenance(
            db,
            project_id=PROJECT_ID,
            resolved_session_id=SESSION_ID,
            source_task_id="#42",
            created_by_agent="codex",
        )

    assert source_task_id == TASK_ID
    assert created_by_agent == "codex"
