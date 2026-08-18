from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.memory.dream import memory_dream
from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.aggregate import (
    _completed_mutation_count,
    _result_run_id,
    _scope_sweep_key,
    _ScopeSweep,
)
from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import list_sweep_candidates, memory_to_candidate
from gobby.memory.dream.models import (
    DreamAction,
    DreamActionName,
    DreamCandidate,
    DreamCheckpoint,
)
from gobby.memory.dream.options import DreamRunOptions, normalize_dream_options
from gobby.memory.dream.orchestrator import (
    MAX_ACTION_SAMPLE,
    WORK_UNIT_MAX_CANDIDATES,
    DreamDependencyError,
    DreamSweepOrchestrator,
    SweepTotals,
    WorkUnitOutcome,
    _decode_raw_plan_metadata,
)
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.planner import (
    PLANNER_TOTAL_DEADLINE_SECONDS,
    _render_candidates_json,
    build_raw_plan,
)
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.service import MemoryDreamService
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.memory.dream.storage_runs import (
    INTERRUPTED_CANCELLED_ERROR,
    INTERRUPTED_RESTART_ERROR,
    PLATFORM_TRUTH_SCOPE,
    RUN_TERMINAL_STATUSES,
)
from gobby.memory.dream.truth_digest import (
    build_current_truth_digest,
    build_project_truth_digest,
    build_project_truth_digest_async,
)
from gobby.memory.generation_schemas import DREAM_ACTIONS_SCHEMA
from gobby.prompts.loader import PromptLoader
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_crud import DuplicateMemoryContentError
from gobby.storage.memories_scope import MemoryScope, MemoryScopeKind
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager

pytestmark = pytest.mark.unit


def _as_hub_db(value: object) -> HubDatabase:
    """Expose a deliberately partial database fake through the production protocol."""
    return cast(HubDatabase, value)


def _as_dream_manager(value: object) -> MemoryDreamManagerProtocol:
    """Expose a focused manager fake through the broader production protocol."""
    return cast(MemoryDreamManagerProtocol, value)


def _planner_db() -> HubDatabase:
    """Return a protocol-shaped DB unused by planner tests without related evidence."""
    return cast(HubDatabase, MagicMock(spec=HubDatabase))


def _dream_store(value: object) -> MemoryDreamStore:
    return MemoryDreamStore(_as_hub_db(value))


def _set_method(target: object, name: str, replacement: object) -> None:
    """Install a test double without weakening the target's static method type."""
    setattr(target, name, replacement)


def _memory(
    memory_id: str,
    *,
    days_old: int = 90,
    access_count: int = 0,
    project_id: str = "proj-1",
    is_global: bool = False,
    last_dreamed_at: str | None = None,
) -> SimpleNamespace:
    when = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    return SimpleNamespace(
        id=memory_id,
        content=f"Memory {memory_id}",
        memory_type="fact",
        project_id=project_id,
        is_global=is_global,
        source_type="agent",
        source_session_id=None,
        tags=[],
        access_count=access_count,
        created_at=when,
        updated_at=when,
        last_accessed_at=datetime.now(UTC).isoformat(),
        deleted_at=None,
        dream_action=None,
        last_dreamed_at=last_dreamed_at,
    )


def _candidate(memory_id: str) -> DreamCandidate:
    created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return DreamCandidate(
        id=memory_id,
        content=f"content {memory_id}",
        memory_type="fact",
        project_id="proj-1",
        is_global=False,
        source_type="agent",
        source_session_id=None,
        tags=[],
        age_days=90,
        access_count=100,
        created_at=created_at,
        updated_at=created_at,
        last_accessed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_prompt_renders_full_content() -> None:
    content = "full candidate content " * 200
    candidate = replace(_candidate("long"), content=content)

    prompt = candidate.to_prompt_dict()

    assert prompt["content"] == content


def test_prompt_dict_related_evidence() -> None:
    from gobby.memory.dream.models import RelatedMemoryEvidence

    evidence_content = "complete newer memory " * 200
    evidence_created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    evidence = RelatedMemoryEvidence(
        id="newer-memory",
        memory_type="fact",
        created_at=evidence_created_at,
        newer_by_days=12.5,
        content=evidence_content,
        matched_via="semantic",
    )

    empty_prompt = _candidate("old-memory").to_prompt_dict()
    related_prompt = replace(_candidate("old-memory"), related=(evidence,)).to_prompt_dict()

    assert "related_newer_memories" not in empty_prompt
    assert related_prompt["related_newer_memories"] == [
        {
            "id": "newer-memory",
            "memory_type": "fact",
            "created_at": evidence_created_at.isoformat(),
            "newer_by_days": 12.5,
            "content": evidence_content,
            "matched_via": "semantic",
        }
    ]
    json.dumps(related_prompt)


class _RecordingSweepSource:
    """Minimal sweep source recording the scope it was queried with."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[Any]:
        self.calls.append(
            {
                "limit": limit,
                "redream_cutoff": redream_cutoff,
                "scope": scope,
                "memory_type": memory_type,
            }
        )
        return list(self.rows)

    def get_memories(self, memory_ids: list[str], scope: MemoryScope) -> list[Any]:
        del memory_ids, scope
        return []


@pytest.mark.asyncio
async def test_list_sweep_candidates_adapts_rows_and_forwards_scope() -> None:
    source = _RecordingSweepSource(
        [
            _memory(
                "21000000-0000-4000-8000-000000000005", last_dreamed_at="2026-01-01T00:00:00+00:00"
            )
        ]
    )

    result = await list_sweep_candidates(
        source,
        limit=50,
        redream_cutoff="2026-06-14T00:00:00+00:00",
        scope=MemoryScope.global_only(),
        memory_type="fact",
        now=datetime(2026, 6, 15, tzinfo=UTC),
    )

    assert [candidate.id for candidate in result] == ["21000000-0000-4000-8000-000000000005"]
    assert source.calls == [
        {
            "limit": 50,
            "redream_cutoff": "2026-06-14T00:00:00+00:00",
            "scope": MemoryScope.global_only(),
            "memory_type": "fact",
        }
    ]
    assert "re-dream cooldown elapsed" in result[0].reasons


@pytest.mark.asyncio
async def test_list_sweep_candidates_flags_never_dreamed_and_global() -> None:
    source = _RecordingSweepSource(
        [_memory("g1", project_id=PERSONAL_PROJECT_ID, is_global=True, last_dreamed_at=None)]
    )

    result = await list_sweep_candidates(
        source,
        limit=10,
        redream_cutoff="2026-06-14T00:00:00+00:00",
        scope=MemoryScope.global_only(),
    )

    assert result[0].project_id == PERSONAL_PROJECT_ID
    assert result[0].is_global is True
    assert result[0].reasons == ["never dreamed", "global memory"]


def test_memory_to_candidate_computes_age_from_updated_at() -> None:
    mem = _memory("aged", days_old=10)

    candidate = memory_to_candidate(mem, datetime.now(UTC))

    assert candidate.id == "aged"
    assert candidate.age_days >= 9.0


@pytest.mark.asyncio
async def test_build_raw_plan_logs_non_dict_actions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_service = MagicMock()
    llm_service.call_json_feature = AsyncMock(
        return_value={"actions": [{"action": "refresh"}, "invalid"]}
    )
    candidate = _candidate("memory-1")

    with patch("gobby.memory.dream.planner.PromptLoader.render", return_value="prompt"):
        plan = await build_raw_plan(
            candidates=[candidate],
            dream_config=SimpleNamespace(prompt_path="memory/dream"),
            llm_service=llm_service,
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert plan["actions"] == [{"action": "refresh"}]
    record = next(
        item
        for item in caplog.records
        if item.message == "Memory dream planner returned non-dict actions"
    )
    assert record.__dict__["invalid_actions"] == ["invalid"]
    assert record.__dict__["project_id"] == "proj-1"
    assert record.__dict__["candidate_ids"] == ["memory-1"]


@pytest.mark.asyncio
async def test_build_raw_plan_batches_candidates_into_pages() -> None:
    candidates = [_candidate(f"m{i}") for i in range(5)]
    planner = AsyncMock(
        side_effect=[
            {"actions": [{"action": "keep", "memory_id": "p0"}]},
            {"actions": [{"action": "keep", "memory_id": "p1"}]},
            {"actions": [{"action": "keep", "memory_id": "p2"}]},
        ]
    )

    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        plan = await build_raw_plan(
            candidates=candidates,
            dream_config=SimpleNamespace(planner_batch_size=2),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert planner.call_count == 3  # ceil(5 / 2)
    page_sizes = sorted(len(call.kwargs["candidates"]) for call in planner.call_args_list)
    assert page_sizes == [1, 2, 2]
    seen = {
        candidate.id for call in planner.call_args_list for candidate in call.kwargs["candidates"]
    }
    assert seen == {f"m{i}" for i in range(5)}
    assert plan["planner_errors"] == []
    assert {action["memory_id"] for action in plan["actions"]} == {"p0", "p1", "p2"}


@pytest.mark.asyncio
async def test_batch_split_guard() -> None:
    candidates = [
        replace(_candidate(f"m{i}"), content=f"complete content {i} " * 20) for i in range(4)
    ]
    single_item_size = len(_render_candidates_json([candidates[0]]))
    planner = AsyncMock(return_value={"actions": []})

    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        plan = await build_raw_plan(
            candidates=candidates,
            dream_config=SimpleNamespace(
                planner_batch_size=4,
                planner_batch_max_chars=single_item_size,
            ),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert planner.call_count == 4
    assert [
        [candidate.id for candidate in call.kwargs["candidates"]] for call in planner.call_args_list
    ] == [["m0"], ["m1"], ["m2"], ["m3"]]
    assert {
        candidate.id: candidate.content
        for call in planner.call_args_list
        for candidate in call.kwargs["candidates"]
    } == {candidate.id: candidate.content for candidate in candidates}
    assert plan["planner_errors"] == []


@pytest.mark.asyncio
async def test_single_item_oversize_dispatches_intact(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.memory.dream.models import RelatedMemoryEvidence

    candidate_content = "complete candidate " * 300
    evidence_content = "complete evidence " * 300
    evidence = RelatedMemoryEvidence(
        id="newer-memory",
        memory_type="fact",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        newer_by_days=1.0,
        content=evidence_content,
        matched_via="semantic",
    )
    candidate = replace(
        _candidate("oversize-memory"),
        content=candidate_content,
        related=(evidence,),
    )
    rendered_size = len(_render_candidates_json([candidate]))
    planner = AsyncMock(return_value={"actions": []})

    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        await build_raw_plan(
            candidates=[candidate],
            dream_config=SimpleNamespace(
                planner_batch_size=1,
                planner_batch_max_chars=rendered_size - 1,
            ),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    planner.assert_awaited_once()
    dispatched = planner.call_args.kwargs["candidates"]
    assert dispatched[0].content == candidate_content
    assert dispatched[0].related[0].content == evidence_content
    assert "oversize-memory" in caplog.text
    assert str(rendered_size) in caplog.text


@pytest.mark.asyncio
async def test_build_raw_plan_isolates_failed_page() -> None:
    candidates = [_candidate(f"m{i}") for i in range(3)]
    planner = AsyncMock(
        side_effect=[
            {"actions": [{"action": "keep", "memory_id": "m0"}]},
            ValueError("page boom"),
            {"actions": [{"action": "keep", "memory_id": "m2"}]},
        ]
    )

    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        plan = await build_raw_plan(
            candidates=candidates,
            dream_config=SimpleNamespace(planner_batch_size=1),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert plan["planner_errors"] == ["page boom"]
    assert {action["memory_id"] for action in plan["actions"]} == {"m0", "m2"}


async def test_build_raw_plan_sends_all_candidates_across_projects() -> None:
    candidates = [
        replace(_candidate("project-a"), content="same", project_id="proj-a"),
        replace(_candidate("project-b"), content="same", project_id="proj-b"),
    ]
    planner = AsyncMock(return_value={"actions": []})

    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        await build_raw_plan(
            candidates=candidates,
            dream_config=SimpleNamespace(planner_batch_size=25),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id=None,
            skip_consolidation=False,
        )

    planned_ids = [candidate.id for candidate in planner.call_args.kwargs["candidates"]]
    assert planned_ids == ["project-a", "project-b"]


@pytest.mark.asyncio
async def test_build_raw_plan_runs_planner_pages_serially() -> None:
    candidates = [_candidate(f"m{i}") for i in range(6)]
    active = 0
    max_active = 0

    async def fake_planner(**_kwargs: Any) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.to_thread(lambda: None)
        active -= 1
        return {"actions": []}

    with patch("gobby.memory.dream.planner._call_llm_planner", fake_planner):
        plan = await build_raw_plan(
            candidates=candidates,
            # Pages run one at a time so Dream can hold at most one host-wide
            # spawn-cold generation slot, leaving the rest for other callers.
            dream_config=SimpleNamespace(planner_batch_size=1),
            llm_service=MagicMock(),
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert max_active == 1  # exactly one planner call in flight at any moment
    assert plan["planner_errors"] == []


@pytest.mark.asyncio
async def test_planner_request_sets_overall_provider_deadline() -> None:
    llm_service = MagicMock()
    llm_service.call_json_feature = AsyncMock(return_value={"actions": []})

    with patch("gobby.memory.dream.planner.PromptLoader.render", return_value="prompt"):
        await build_raw_plan(
            candidates=[_candidate("memory-1")],
            dream_config=SimpleNamespace(prompt_path="memory/dream"),
            llm_service=llm_service,
            db=_planner_db(),
            project_id="proj-1",
            skip_consolidation=False,
        )

    kwargs = llm_service.call_json_feature.await_args.kwargs
    assert kwargs["json_schema"] == DREAM_ACTIONS_SCHEMA
    assert kwargs["total_timeout_seconds"] == PLANNER_TOTAL_DEADLINE_SECONDS
    assert PLANNER_TOTAL_DEADLINE_SECONDS == 1200.0


def test_plan_validation_degrades_bad_or_omitted_actions_to_keep() -> None:
    candidates = [_candidate("a"), _candidate("b"), _candidate("c"), _candidate("d")]
    raw_plan = {
        "actions": [
            {"action": "delete", "memory_id": "a", "confidence": 0.2},
            {"action": "refresh", "memory_id": "b", "confidence": 0.9},
            {"action": "delete", "memory_id": "missing", "confidence": 1.0},
            {"action": "explode", "memory_id": "c", "confidence": 1.0},
        ]
    }

    actions = validate_dream_plan(
        raw_plan,
        candidates,
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
    )

    by_id = {action.memory_id: action for action in actions if action.memory_id}
    # Low-confidence delete, content-less refresh, unknown action, and the
    # omitted candidate all degrade to visible keep — never a hide.
    assert by_id["a"].action == "keep"
    assert by_id["a"].reason == "confidence below mutation threshold"
    assert by_id["b"].action == "keep"
    assert by_id["b"].reason == "refresh requires replacement content"
    assert by_id["c"].action == "keep"
    assert by_id["c"].reason == "unknown action"
    assert by_id["d"].action == "keep"
    assert by_id["d"].reason == "candidate omitted from dream plan"


def test_plan_validation_keeps_invalid_and_missing_id_with_reasons() -> None:
    actions = validate_dream_plan(
        {
            "actions": [
                {"action": "delete", "memory_id": "missing", "confidence": 1.0},
                {"action": "delete", "confidence": 1.0},
            ]
        },
        [_candidate("a")],
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
    )

    no_id = [action for action in actions if action.memory_id is None]
    assert [action.reason for action in no_id] == ["unknown candidate id", "missing candidate id"]
    assert all(action.action == "keep" for action in no_id)
    assert any(
        action.memory_id == "a"
        and action.action == "keep"
        and action.reason == "candidate omitted from dream plan"
        for action in actions
    )


def test_plan_validation_degrades_overlapping_actions_to_keep() -> None:
    actions = validate_dream_plan(
        {
            "actions": [
                {"action": "delete", "memory_id": "a", "confidence": 1.0},
                {"action": "delete", "memory_id": "a", "confidence": 1.0},
            ]
        },
        [_candidate("a"), _candidate("b")],
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
    )

    assert any(action.memory_id == "a" and action.action == "delete" for action in actions)
    assert any(
        action.memory_id == "a"
        and action.action == "keep"
        and action.reason == "candidate had overlapping dream actions"
        for action in actions
    )
    assert any(
        action.memory_id == "b"
        and action.action == "keep"
        and action.reason == "candidate omitted from dream plan"
        for action in actions
    )


def test_plan_validation_promote_uses_rescope_threshold() -> None:
    actions = validate_dream_plan(
        {
            "actions": [
                {"action": "promote", "memory_id": "low", "confidence": 0.84},
                {"action": "promote", "memory_id": "high", "confidence": 0.9},
            ]
        },
        [_candidate("low"), _candidate("high")],
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
    )

    by_id = {action.memory_id: action for action in actions if action.memory_id}
    assert by_id["low"].action == "keep"
    assert by_id["low"].reason == "confidence below mutation threshold"
    assert by_id["high"].action == "promote"


def test_malformed_plan_keeps_all_candidates() -> None:
    actions = validate_dream_plan(
        "not-json",
        [_candidate("a"), _candidate("b")],
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
    )

    # A malformed plan must never hide a memory: every candidate degrades to keep.
    assert {action.memory_id for action in actions} == {"a", "b"}
    assert {action.action for action in actions} == {"keep"}
    assert all(action.reason == "candidate omitted from dream plan" for action in actions)


@pytest.mark.asyncio
async def test_apply_and_revert_soft_hide_refresh_and_keep() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "hide-me": _row("hide-me", "junk"),
        "review-me": _row("review-me", "ambiguous"),
        "refresh-me": _row("refresh-me", "old"),
        "keep-me": _row("keep-me", "durable"),
    }
    db.crossrefs[("hide-me", "keep-me")] = {
        "source_id": "hide-me",
        "target_id": "keep-me",
        "similarity": 0.91,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    actions = [
        DreamAction(action="delete", memory_id="hide-me", confidence=1),
        DreamAction(action="review", memory_id="review-me", confidence=1),
        DreamAction(action="refresh", memory_id="refresh-me", content="new", confidence=1),
        DreamAction(action="keep", memory_id="keep-me", confidence=1),
    ]

    summary = await apply_dream_plan(
        memory_manager=_as_dream_manager(manager),
        store=store,
        run_id=run_id,
        actions=actions,
        candidates=[
            _candidate("hide-me"),
            _candidate("review-me"),
            _candidate("refresh-me"),
            _candidate("keep-me"),
        ],
        dry_run=False,
        reconcile_after_apply=False,
    )

    # keep is not a mutation; delete/review/refresh each count once.
    assert summary["mutations"] == 3
    assert summary["errors"] == 0
    # delete and review soft-hide rather than physically removing the row.
    assert db.memories["hide-me"]["deleted_at"] is not None
    assert db.memories["hide-me"]["dream_action"] == "delete"
    assert db.memories["review-me"]["deleted_at"] is not None
    assert db.memories["review-me"]["dream_action"] == "review"
    assert db.memories["refresh-me"]["content"] == "new"
    assert db.memories["keep-me"]["deleted_at"] is None
    # every candidate on the page is stamped, including the keep.
    assert all(
        db.memories[mid]["last_dreamed_at"] is not None
        for mid in ("hide-me", "review-me", "refresh-me", "keep-me")
    )
    assert {row["action"] for row in db.snapshots} == {
        "delete",
        "review",
        "refresh",
        "keep",
    }

    # Simulate a vectorless restored row: the direct restore hook repairs only
    # graph state, so the post-revert reconciliation must backfill the vector.
    manager.vector_ids.discard("refresh-me")

    async def restore_graph_only(
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
        memory_type: str,
        *,
        notify_changed: bool = True,
    ) -> bool:
        del content, project_id, is_global, memory_type, notify_changed
        manager.graph_ids.add(memory_id)
        return True

    async def reconcile_missing_vectors(dry_run: bool = False) -> dict[str, Any]:
        missing = set(db.memories) - manager.vector_ids
        if not dry_run:
            manager.vector_ids.update(missing)
        return {
            "qdrant": {
                "missing_found": len(missing),
                "missing_embedded": 0 if dry_run else len(missing),
            }
        }

    manager.restore_memory_indices.side_effect = restore_graph_only
    manager.reconcile_stores.side_effect = reconcile_missing_vectors

    result = await revert_dream_run(
        store=store, run_id=run_id, memory_manager=_as_dream_manager(manager)
    )

    assert result["success"] is True
    # Revert restores the mutating snapshots (delete/review/refresh) to active.
    assert db.memories["hide-me"]["deleted_at"] is None
    assert db.memories["hide-me"]["dream_action"] is None
    assert db.memories["review-me"]["deleted_at"] is None
    assert db.memories["refresh-me"]["content"] == "old"
    assert set(db.crossrefs) == {("hide-me", "keep-me")}
    restored_ids = {call.args[0] for call in manager.restore_memory_indices.await_args_list}
    assert {"hide-me", "review-me", "refresh-me"} <= restored_ids
    assert {"hide-me", "review-me", "refresh-me"} <= manager.vector_ids
    assert {"hide-me", "review-me", "refresh-me"} <= manager.graph_ids
    assert result["reconcile"]["qdrant"] == {"missing_found": 1, "missing_embedded": 1}
    manager.reconcile_stores.assert_awaited_once_with(dry_run=False)


@pytest.mark.asyncio
async def test_apply_promote_preserves_updated_at_and_revert_rescopes() -> None:
    db = _FakeDreamDB()
    db.memories = {"promote-me": _row("promote-me", "universal")}
    before_updated_at = db.memories["promote-me"]["updated_at"]
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=_as_dream_manager(manager),
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="promote", memory_id="promote-me", confidence=0.9)],
        candidates=[_candidate("promote-me")],
        dry_run=False,
        reconcile_after_apply=False,
        when="2026-01-01T00:00:00+00:00",
    )

    assert summary["mutations"] == 1
    assert db.memories["promote-me"]["project_id"] == "proj-1"
    assert db.memories["promote-me"]["is_global"] is True
    assert db.memories["promote-me"]["updated_at"] == before_updated_at
    assert db.memories["promote-me"]["last_dreamed_at"] == "2026-01-01T00:00:00+00:00"
    assert {row["action"] for row in db.snapshots} == {"promote"}
    assert any(
        call.args[0].id == "promote-me" and call.args[0].is_global is True
        for call in manager.sync_memory_scope_indices.await_args_list
    )

    result = await revert_dream_run(
        store=store, run_id=run_id, memory_manager=_as_dream_manager(manager)
    )

    assert result["success"] is True
    assert db.memories["promote-me"]["project_id"] == "proj-1"
    assert db.memories["promote-me"]["is_global"] is False
    assert db.memories["promote-me"]["updated_at"] > before_updated_at
    assert any(
        call.args[0].id == "promote-me" and call.args[0].is_global is False
        for call in manager.sync_memory_scope_indices.await_args_list
    )


@pytest.mark.asyncio
async def test_apply_dream_plan_dry_run_includes_planned_action_preview() -> None:
    db = _FakeDreamDB()
    db.memories = {"memory-1": _row("memory-1", "old")}
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=True, options={})

    summary = await apply_dream_plan(
        memory_manager=_as_dream_manager(manager),
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="refresh", memory_id="memory-1", content="new")],
        candidates=[_candidate("memory-1")],
        dry_run=True,
        reconcile_after_apply=False,
    )

    assert summary["mutations"] == 0
    assert summary["snapshots"] == 0
    assert db.memories["memory-1"]["content"] == "old"
    assert summary["planned_actions"] == [
        {
            "action": {
                "action": "refresh",
                "memory_id": "memory-1",
                "memory_ids": [],
                "content": "new",
                "target_id": None,
                "memory_type": None,
                "tags": None,
                "reason": "",
                "confidence": 0.0,
            },
            "affected_ids": ["memory-1"],
            "candidates": [_candidate("memory-1").to_prompt_dict()],
        }
    ]


@pytest.mark.asyncio
async def test_apply_dream_plan_records_error_for_empty_memory_id() -> None:
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=_as_dream_manager(manager),
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="delete", memory_id="")],
        candidates=[],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert summary["errors"] == 1
    assert summary["mutations"] == 0
    assert "requires non-empty memory_id" in summary["error_details"][0]["error"]


async def test_revert_dream_run_uses_newest_first_snapshots_without_reversal() -> None:
    db = _FakeDreamDB()
    db.memories = {"memory-1": _row("memory-1", "v3")}
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    first_snapshot = store.insert_snapshot(
        run_id=run_id,
        memory_id="memory-1",
        action="refresh",
        before_data=_row("memory-1", "v1"),
    )
    store.complete_snapshot(first_snapshot, after_data=_row("memory-1", "v2"))
    second_snapshot = store.insert_snapshot(
        run_id=run_id,
        memory_id="memory-1",
        action="refresh",
        before_data=_row("memory-1", "v2"),
    )
    store.complete_snapshot(second_snapshot, after_data=_row("memory-1", "v3"))

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is True
    assert db.memories["memory-1"]["content"] == "v1"


@pytest.mark.asyncio
async def test_revert_dream_run_fails_closed_when_snapshots_were_forfeited() -> None:
    db = _FakeDreamDB()
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    store.update_run(run_id, status="revert_forfeited")

    with patch.object(store, "list_snapshots", wraps=store.list_snapshots) as list_snapshots:
        result = await revert_dream_run(store=store, run_id=run_id)

    assert result == {
        "success": False,
        "run_id": run_id,
        "status": "revert_forfeited",
        "error": "Dream run cannot be reverted because its snapshots were forfeited",
    }
    list_snapshots.assert_not_called()
    forfeited_run = store.get_run(run_id)
    assert forfeited_run is not None
    assert forfeited_run["status"] == "revert_forfeited"


@pytest.mark.asyncio
async def test_revert_dream_run_marks_revert_failed_and_continues_on_snapshot_error() -> None:
    db = _FakeDreamDB()
    db.memories = {"good": _row("good", "new")}
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    db.snapshots = [
        {
            "id": 1,
            "run_id": run_id,
            "memory_id": "bad",
            "action": "refresh",
            "before_data": {"id": "bad"},
            "after_data": None,
            "applied": True,
        },
        {
            "id": 2,
            "run_id": run_id,
            "memory_id": "good",
            "action": "refresh",
            "before_data": _row("good", "old"),
            "after_data": _row("good", "new"),
            "applied": True,
        },
    ]

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is False
    assert result["status"] == "revert_failed"
    assert result["restored"] == 1
    assert result["errors"] == 1
    assert db.memories["good"]["content"] == "old"
    assert db.runs[run_id]["status"] == "revert_failed"
    run = store.get_run(run_id)
    assert run is not None
    assert run["summary"]["errors"] == 1


@pytest.mark.asyncio
async def test_revert_dream_run_records_malformed_snapshot_without_key_error() -> None:
    db = _FakeDreamDB()
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    db.snapshots = [
        {
            "id": 1,
            "run_id": run_id,
            "action": "delete",
            "before_data": None,
            "after_data": _row("created-1", "new"),
            "applied": True,
        }
    ]

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is False
    assert result["status"] == "revert_failed"
    assert result["error_details"] == [{"snapshot_id": 1, "error": "snapshot missing memory_id"}]


async def test_memory_dream_service_revert_uses_reconcile_after_revert_config() -> None:
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=MemoryDreamConfig(reconcile_after_revert=False),
    )
    revert_mock = AsyncMock(return_value={"success": True, "run_id": "dream-1"})

    with patch("gobby.memory.dream.service.revert_dream_run", revert_mock):
        result = await service.revert("dream-1")

    assert result["success"] is True
    assert revert_mock.await_args is not None
    assert revert_mock.await_args.kwargs["reconcile_after_revert"] is False


def test_memory_dream_service_record_run_failure_is_idempotent() -> None:
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager), dream_config=MemoryDreamConfig()
    )
    run_id = service.store.create_run(project_id="proj-1", dry_run=False, options={})

    failed = service.record_run_failure(run_id, "boom")
    repeated = service.record_run_failure(run_id, "later")

    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"
    assert repeated is not None
    assert repeated["error"] == "boom"


@pytest.mark.asyncio
async def test_memory_dream_service_persists_interrupted_status_on_cancellation() -> None:
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager), dream_config=MemoryDreamConfig()
    )
    run_id = service.store.create_run(project_id=None, dry_run=False, options={})
    _set_method(service, "_stream_sweep", AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await service.execute_run(run_id, DreamRunOptions(dry_run=False, project_id="proj-1"))

    run = service.store.get_run(run_id)
    assert run is not None
    assert run["status"] == "interrupted"
    assert run["error"] == INTERRUPTED_CANCELLED_ERROR
    assert run["completed_at"] is not None


def _execution_test_service() -> MemoryDreamService:
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeMemoryManager(_FakeDreamDB())),
        dream_config=MemoryDreamConfig(
            enabled=True,
            planner_batch_size=2,
            redream_after_hours=20,
            include_global_memories=False,
            reconcile_after_apply=False,
        ),
    )
    _set_method(service, "_build_truth_digest_async", AsyncMock(return_value=""))
    return service


def _empty_sweep_totals() -> MagicMock:
    totals = MagicMock(mutations=0)
    totals.to_plan.return_value = {}
    totals.to_summary.return_value = {}
    return totals


async def test_dream_execution_lock_queues_across_service_instances() -> None:
    first_service = _execution_test_service()
    second_service = _execution_test_service()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_sweep(*_args: Any, **_kwargs: Any) -> MagicMock:
        first_entered.set()
        await release_first.wait()
        return _empty_sweep_totals()

    async def second_sweep(*_args: Any, **_kwargs: Any) -> MagicMock:
        second_entered.set()
        return _empty_sweep_totals()

    async def run_second() -> dict[str, Any]:
        second_attempted.set()
        return await second_service.execute_run(second_run_id, options)

    _set_method(first_service, "_stream_sweep", AsyncMock(side_effect=first_sweep))
    _set_method(second_service, "_stream_sweep", AsyncMock(side_effect=second_sweep))
    first_run_id = first_service.store.create_run(project_id="proj-1", dry_run=False, options={})
    # Only one row may hold 'running'; the queued execution records as 'started'.
    second_run_id = second_service.store.create_run(
        project_id="proj-1", dry_run=False, options={}, status="started"
    )
    options = DreamRunOptions(dry_run=False, project_id="proj-1")

    first_task = asyncio.create_task(first_service.execute_run(first_run_id, options))
    await first_entered.wait()
    second_task = asyncio.create_task(run_second())
    await second_attempted.wait()

    assert second_entered.is_set() is False
    release_first.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result["success"] is True
    assert second_result["success"] is True
    assert second_entered.is_set() is True


async def test_dream_execution_lock_covers_aggregate_cron_entrypoint() -> None:
    aggregate_service = _execution_test_service()
    manual_service = _execution_test_service()
    aggregate_entered = asyncio.Event()
    release_aggregate = asyncio.Event()
    manual_attempted = asyncio.Event()
    manual_entered = asyncio.Event()

    async def blocked_truth_trigger() -> None:
        aggregate_entered.set()
        await release_aggregate.wait()

    async def manual_sweep(*_args: Any, **_kwargs: Any) -> MagicMock:
        manual_entered.set()
        return _empty_sweep_totals()

    async def run_manual() -> dict[str, Any]:
        manual_attempted.set()
        return await manual_service.execute_run(manual_run_id, options)

    _set_method(
        aggregate_service,
        "_apply_truth_change_triggers",
        AsyncMock(side_effect=blocked_truth_trigger),
    )
    _set_method(aggregate_service.memory_manager, "list_dream_scopes", MagicMock(return_value=[]))
    _set_method(manual_service, "_stream_sweep", AsyncMock(side_effect=manual_sweep))
    manual_run_id = manual_service.store.create_run(project_id="proj-1", dry_run=False, options={})
    options = DreamRunOptions(dry_run=False, project_id="proj-1")

    aggregate_task = asyncio.create_task(aggregate_service.run_all_due_projects())
    await aggregate_entered.wait()
    manual_task = asyncio.create_task(run_manual())
    await manual_attempted.wait()

    assert manual_entered.is_set() is False
    release_aggregate.set()
    aggregate_result, manual_result = await asyncio.gather(aggregate_task, manual_task)
    assert aggregate_result["success"] is True
    assert manual_result["success"] is True
    assert manual_entered.is_set() is True


async def test_dream_execution_lock_releases_after_failure() -> None:
    failing_service = _execution_test_service()
    next_service = _execution_test_service()
    _set_method(
        failing_service,
        "_stream_sweep",
        AsyncMock(side_effect=RuntimeError("dream failed")),
    )
    _set_method(next_service, "_stream_sweep", AsyncMock(return_value=_empty_sweep_totals()))
    options = DreamRunOptions(dry_run=False, project_id="proj-1")
    failing_run_id = failing_service.store.create_run(
        project_id="proj-1", dry_run=False, options={}
    )
    next_run_id = next_service.store.create_run(project_id="proj-1", dry_run=False, options={})

    failed = await failing_service.execute_run(failing_run_id, options)
    succeeded = await asyncio.wait_for(next_service.execute_run(next_run_id, options), timeout=1)

    assert failed["success"] is False
    assert succeeded["success"] is True


async def test_dream_execution_lock_releases_after_cancellation() -> None:
    cancelled_service = _execution_test_service()
    next_service = _execution_test_service()
    entered = asyncio.Event()

    async def blocked_sweep(*_args: Any, **_kwargs: Any) -> MagicMock:
        entered.set()
        await asyncio.Event().wait()
        return _empty_sweep_totals()

    _set_method(cancelled_service, "_stream_sweep", AsyncMock(side_effect=blocked_sweep))
    _set_method(next_service, "_stream_sweep", AsyncMock(return_value=_empty_sweep_totals()))
    options = DreamRunOptions(dry_run=False, project_id="proj-1")
    cancelled_run_id = cancelled_service.store.create_run(
        project_id="proj-1", dry_run=False, options={}
    )
    next_run_id = next_service.store.create_run(project_id="proj-1", dry_run=False, options={})
    cancelled_task = asyncio.create_task(cancelled_service.execute_run(cancelled_run_id, options))
    await entered.wait()

    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    succeeded = await asyncio.wait_for(next_service.execute_run(next_run_id, options), timeout=1)

    assert succeeded["success"] is True


async def test_run_coalesces_onto_active_covering_run_without_executing() -> None:
    service = _execution_test_service()
    sweep = AsyncMock(return_value=_empty_sweep_totals())
    _set_method(service, "_stream_sweep", sweep)
    active_id = service.store.create_run(
        project_id=None,
        dry_run=False,
        options={"aggregate": True, "dry_run": False},
    )

    result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is True
    assert result["coalesced"] is True
    assert result["run_id"] == active_id
    assert result["active"]["scope"] == "all"
    sweep.assert_not_awaited()


async def test_run_conflicts_with_incompatible_active_run_without_executing() -> None:
    service = _execution_test_service()
    sweep = AsyncMock(return_value=_empty_sweep_totals())
    _set_method(service, "_stream_sweep", sweep)
    active_id = service.store.create_run(
        project_id="proj-1",
        dry_run=False,
        options=DreamRunOptions(dry_run=False, project_id="proj-1").to_dict(),
    )

    result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-2"))

    assert result["success"] is False
    assert result["conflict"]["run_id"] == active_id
    assert result["conflict"]["scope"] == "project:proj-1"
    sweep.assert_not_awaited()


def test_postgres_admission_admits_then_coalesces_equivalent(temp_db: Any) -> None:
    store = MemoryDreamStore(temp_db)
    options = DreamRunOptions(dry_run=False, project_id=PERSONAL_PROJECT_ID).to_dict()

    admitted = store.admit_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options=options)
    coalesced = store.admit_run(
        project_id=PERSONAL_PROJECT_ID, dry_run=False, options=dict(options)
    )

    assert admitted.outcome == "admitted"
    assert admitted.run_id is not None
    assert coalesced.outcome == "coalesced"
    assert coalesced.run_id == admitted.run_id
    assert coalesced.active is not None
    assert coalesced.active["scope"] == f"project:{PERSONAL_PROJECT_ID}"


def test_postgres_admission_all_due_covers_project_request(temp_db: Any) -> None:
    store = MemoryDreamStore(temp_db)
    aggregate_options = {"aggregate": True, "dry_run": False}
    admitted = store.admit_run(project_id=None, dry_run=False, options=aggregate_options)
    assert admitted.outcome == "admitted"

    covered = store.admit_run(
        project_id=PERSONAL_PROJECT_ID,
        dry_run=False,
        options=DreamRunOptions(dry_run=False, project_id=PERSONAL_PROJECT_ID).to_dict(),
    )
    narrowed = store.admit_run(
        project_id=PERSONAL_PROJECT_ID,
        dry_run=False,
        options=DreamRunOptions(
            dry_run=False, project_id=PERSONAL_PROJECT_ID, include_global=False
        ).to_dict(),
    )

    assert covered.outcome == "coalesced"
    assert covered.run_id == admitted.run_id
    # include_global=False narrows the sweep incompatibly: the all-due run
    # does not honor a global-bucket exclusion.
    assert narrowed.outcome == "conflict"
    assert narrowed.active is not None
    assert narrowed.active["run_id"] == admitted.run_id
    assert narrowed.active["scope"] == "all"


def test_postgres_admission_conflict_creates_no_row_and_returns_details(temp_db: Any) -> None:
    store = MemoryDreamStore(temp_db)
    active_options = DreamRunOptions(dry_run=False, project_id=PERSONAL_PROJECT_ID).to_dict()
    admitted = store.admit_run(
        project_id=PERSONAL_PROJECT_ID, dry_run=False, options=active_options
    )
    assert admitted.run_id is not None
    store.update_run(admitted.run_id, checkpoint={"phase": "sweep", "batch_number": 2})

    conflict = store.admit_run(
        project_id=None,
        dry_run=True,
        options=DreamRunOptions(dry_run=True, global_only=True).to_dict(),
    )

    assert conflict.outcome == "conflict"
    assert conflict.run_id is None
    assert conflict.active is not None
    assert conflict.active["run_id"] == admitted.run_id
    assert conflict.active["scope"] == f"project:{PERSONAL_PROJECT_ID}"
    assert conflict.active["phase"] == "sweep"
    assert conflict.active["checkpoint"] == {"phase": "sweep", "batch_number": 2}
    assert conflict.active["options"] == normalize_dream_options(active_options)
    rows = temp_db.fetchall("SELECT id, status FROM memory_dream_runs", ())
    assert [str(row["id"]) for row in rows] == [admitted.run_id]


def test_postgres_admission_race_admits_exactly_one(temp_db: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = MemoryDreamStore(temp_db)
    options = {"aggregate": True, "dry_run": True}

    def admit() -> str:
        return store.admit_run(project_id=None, dry_run=True, options=options).outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _: admit(), range(2)))

    assert outcomes == ["admitted", "coalesced"]
    rows = temp_db.fetchall("SELECT id FROM memory_dream_runs WHERE status = 'running'", ())
    assert len(rows) == 1


@pytest.mark.parametrize("terminal_status", sorted(RUN_TERMINAL_STATUSES))
def test_postgres_admission_reopens_after_terminal_status(
    temp_db: Any,
    terminal_status: str,
) -> None:
    store = MemoryDreamStore(temp_db)
    options = {"aggregate": True, "dry_run": False}
    first = store.admit_run(project_id=None, dry_run=False, options=options)
    assert first.run_id is not None
    store.update_run(first.run_id, status=terminal_status)

    second = store.admit_run(project_id=None, dry_run=False, options=options)

    assert first.outcome == "admitted"
    assert second.outcome == "admitted"
    assert second.run_id != first.run_id


def test_postgres_partial_run_checkpoint_round_trip(temp_db: Any) -> None:
    store = MemoryDreamStore(temp_db)
    admitted = store.admit_run(
        project_id=None, dry_run=False, options={"aggregate": True, "dry_run": False}
    )
    assert admitted.run_id is not None
    checkpoint = DreamCheckpoint(
        phase="sweep",
        scope="all",
        pass_number=2,
        batch_number=7,
        selected=25,
        completed=175,
        skipped_fence=3,
        remaining=50,
        channels={"keyword": {"attempts": 2, "latency_ms": 812.5}},
        planned=170,
        actions=160,
        mutations=41,
        backlog={"project:abc": 25, "global": 25},
        stop_reason="run_ceiling",
        last_dependency_failure="planner timeout on unit 6",
    )

    store.update_run(admitted.run_id, status="partial", checkpoint=checkpoint.to_dict())

    assert "partial" in RUN_TERMINAL_STATUSES
    run = store.get_run(admitted.run_id)
    assert run is not None
    assert run["status"] == "partial"
    assert run["checkpoint"] == checkpoint.to_dict()


def test_postgres_restart_recovery_interrupts_and_frees_admission(temp_db: Any) -> None:
    store = MemoryDreamStore(temp_db)
    admitted = store.admit_run(
        project_id=None, dry_run=False, options={"aggregate": True, "dry_run": False}
    )
    child_id = store.create_run(
        project_id=PERSONAL_PROJECT_ID,
        dry_run=False,
        options=DreamRunOptions(dry_run=False, project_id=PERSONAL_PROJECT_ID).to_dict(),
        status="started",
    )
    memory = LocalMemoryManager(temp_db).create_memory(
        content="committed action", project_id=PERSONAL_PROJECT_ID
    )
    snapshot_id = store.insert_snapshot(
        run_id=child_id, memory_id=memory.id, action="refresh", before_data={"id": memory.id}
    )
    store.complete_snapshot(snapshot_id, after_data={"id": memory.id})

    reconciled = store.mark_interrupted_runs()

    assert admitted.run_id is not None
    assert set(reconciled) == {admitted.run_id, child_id}
    for run_id in (admitted.run_id, child_id):
        run = store.get_run(run_id)
        assert run is not None
        assert run["status"] == "interrupted"
        assert run["error"] == INTERRUPTED_RESTART_ERROR
    # Committed snapshots survive recovery untouched: a later run continues
    # from naturally due candidates instead of replaying applied actions.
    snapshots = store.list_snapshots(child_id)
    assert [snapshot["id"] for snapshot in snapshots] == [snapshot_id]
    readmitted = store.admit_run(
        project_id=None, dry_run=False, options={"aggregate": True, "dry_run": False}
    )
    assert readmitted.outcome == "admitted"


def test_dream_action_vocabulary_matches_live_pipeline() -> None:
    assert set(get_args(DreamActionName)) == {"keep", "delete", "refresh", "review", "promote"}


def test_baseline_admission_contract_matches_runtime_status_vocabulary() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    baseline = (repo_root / "crates/gcore/assets/schema/baseline.sql").read_text(encoding="utf-8")

    for fragment in ("'partial'", "'revert_forfeited'"):
        assert fragment in baseline
    for status in sorted(RUN_TERMINAL_STATUSES | {"started", "running"}):
        assert f"'{status}'" in baseline, f"baseline is missing status {status!r}"


def test_decode_raw_plan_metadata_handles_strings_safely() -> None:
    assert _decode_raw_plan_metadata('{"planner_errors": ["missing llm"]}') == {
        "planner_errors": ["missing llm"]
    }
    assert _decode_raw_plan_metadata("{bad") == {}
    assert _decode_raw_plan_metadata("[1, 2]") == {}


def _row(memory_id: str, content: str) -> dict[str, Any]:
    return {
        "id": memory_id,
        "project_id": "proj-1",
        "is_global": False,
        "memory_type": "fact",
        "content": content,
        "source_type": "agent",
        "source_session_id": None,
        "access_count": 0,
        "last_accessed_at": None,
        "tags": [],
        "graph_processed": True,
        # datetime objects, not ISO strings: psycopg dict_row returns
        # TIMESTAMPTZ columns as datetimes, and snapshots must survive that.
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
        "deleted_at": None,
        "dream_action": None,
        "last_dreamed_at": None,
    }


def _project_row(project_id: str, repo_path: Path | None) -> dict[str, Any]:
    return {
        "id": project_id,
        "name": project_id,
        "repo_path": str(repo_path) if repo_path is not None else None,
        "github_url": None,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
    }


def _write_truth_digest(repo_path: Path, payload: dict[str, Any]) -> Path:
    vault = repo_path / "wiki"
    marker = vault / "_gwiki" / "scope.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("{}\n", encoding="utf-8")
    digest_path = vault / "_meta" / "truth_digest.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps(payload), encoding="utf-8")
    return digest_path


def _complete_digest_payload(service: str = "React UI") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "project_id": "project-1",
        "repo_summary": "A small web frontend with generated docs.",
        "stack_authority": "complete_current_set",
        "stack": [
            {
                "service": service,
                "kind": "frontend",
                "adapter_module": "src/app.tsx:10",
                "pulled_in_by": ["package.json"],
                "summary": "Browser application surface.",
                "degradation": "Build fails when dependencies are missing.",
            }
        ],
        "key_paths": {service: "src/app.tsx:10"},
    }


async def _capture_service_truth_digest(
    db: _FakeDreamDB,
    options: DreamRunOptions,
    *,
    current_project_id: str | None = None,
) -> str:
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10),
        llm_service=MagicMock(),
        current_project_id=current_project_id,
    )
    captured: dict[str, str] = {}

    async def fake_plan(**kwargs: Any) -> dict[str, Any]:
        captured["truth_digest"] = str(kwargs["truth_digest"])
        return {"actions": [], "planner_errors": []}

    with patch("gobby.memory.dream.orchestrator.build_raw_plan", side_effect=fake_plan):
        result = await service.run(options)

    assert result["success"] is True
    return captured["truth_digest"]


class _Cursor:
    rowcount = 1


_MEMORY_TIMESTAMPTZ_COLUMNS = (
    "last_accessed_at",
    "created_at",
    "updated_at",
    "deleted_at",
    "last_dreamed_at",
)


class _FakeDreamDB:
    dialect = "postgres"

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}
        self.crossrefs: dict[tuple[str, str], dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.projection_changes: list[dict[str, Any]] = []

    def transaction(self) -> Any:
        @contextlib.contextmanager
        def _txn() -> Any:
            memories = copy.deepcopy(self.memories)
            crossrefs = copy.deepcopy(self.crossrefs)
            snapshots = copy.deepcopy(self.snapshots)
            projection_changes = copy.deepcopy(self.projection_changes)
            try:
                yield _FencedConn(self)
            except Exception:
                self.memories = memories
                self.crossrefs = crossrefs
                self.snapshots = snapshots
                self.projection_changes = projection_changes
                raise

        return _txn()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO memory_dream_runs"):
            self.runs[params[0]] = {
                "id": params[0],
                "project_id": params[1],
                "status": params[2],
                "dry_run": params[3],
                "options": params[4],
                "started_at": params[5],
                # created_at/updated_at come from the DB DEFAULT now()
                "created_at": params[5],
                "updated_at": params[5],
                "plan": None,
                "summary": None,
                "error": None,
                "completed_at": None,
                "reverted_at": None,
            }
        elif normalized.startswith("UPDATE memory_dream_snapshots"):
            if not params:
                return _Cursor()
            snapshot = self._snapshot(int(params[1]))
            snapshot["after_data"] = params[0]
            snapshot["applied"] = True
        elif normalized.startswith("UPDATE memory_dream_runs"):
            if not params:
                return _Cursor()
            run = self.runs[str(params[-1])]
            assignments = normalized.split(" SET ", maxsplit=1)[1].split(" WHERE ", maxsplit=1)[0]
            for index, assignment in enumerate(assignments.split(", ")):
                column = assignment.split(" = ", maxsplit=1)[0].strip()
                run[column] = params[index]
        elif normalized.startswith("INSERT INTO memory_dream_snapshots"):
            self.snapshots.append(
                {
                    "id": len(self.snapshots) + 1,
                    "run_id": params[0],
                    "memory_id": params[1],
                    "action": params[2],
                    "before_data": params[3],
                    "after_data": params[4] if len(params) > 4 else None,
                    "applied": True,
                }
            )
        elif normalized.startswith("DELETE FROM memory_crossrefs"):
            memory_id = str(params[0])
            self.crossrefs = {
                key: row for key, row in self.crossrefs.items() if memory_id not in key
            }
        elif normalized.startswith("INSERT INTO memory_crossrefs"):
            key = (str(params[0]), str(params[1]))
            row = {
                "source_id": key[0],
                "target_id": key[1],
                "similarity": float(params[2]),
                "created_at": params[3],
            }
            if "GREATEST" in normalized and key in self.crossrefs:
                row["similarity"] = max(
                    float(self.crossrefs[key]["similarity"]),
                    row["similarity"],
                )
            self.crossrefs[key] = row
        elif normalized.startswith("DELETE FROM memories"):
            self.memories.pop(str(params[0]), None)
        elif normalized.startswith("INSERT INTO memories"):
            columns = (
                "id",
                "project_id",
                "is_global",
                "memory_type",
                "content",
                "source_type",
                "source_session_id",
                "access_count",
                "last_accessed_at",
                "tags",
                "graph_processed",
                "created_at",
                "updated_at",
                "deleted_at",
                "dream_action",
                "last_dreamed_at",
            )
            row = dict(zip(columns, params, strict=True))
            # Postgres casts text params bound to TIMESTAMPTZ columns, so a
            # restored row reads back with datetime values; mirror that here.
            for column in _MEMORY_TIMESTAMPTZ_COLUMNS:
                value = row[column]
                if isinstance(value, str):
                    row[column] = datetime.fromisoformat(value)
            self.memories[str(params[0])] = row
        return _Cursor()

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT COUNT(*) AS total FROM memory_dream_snapshots"):
            run_id = str(params[0])
            total = sum(1 for row in self.snapshots if row["run_id"] == run_id and row["applied"])
            return {"total": total}
        if normalized.startswith("INSERT INTO memory_dream_snapshots"):
            snapshot = {
                "id": len(self.snapshots) + 1,
                "run_id": params[0],
                "memory_id": params[1],
                "action": params[2],
                "before_data": params[3],
                "after_data": None,
                "applied": False,
            }
            self.snapshots.append(snapshot)
            return {"id": snapshot["id"]}
        if normalized.startswith("SELECT * FROM memory_dream_runs WHERE status = 'running'"):
            for run in self.runs.values():
                if run.get("status") == "running":
                    return dict(run)
            return None
        if normalized.startswith("SELECT * FROM memory_dream_runs"):
            return self.runs.get(str(params[0]))
        if "FROM projects" in normalized:
            row = self.projects.get(str(params[0]))
            return dict(row) if row else None
        if "FROM memories" in normalized:
            row = self.memories.get(str(params[0]))
            return dict(row) if row else None
        return None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if "FROM memory_crossrefs" in normalized:
            memory_id = str(params[0])
            return [dict(row) for key, row in self.crossrefs.items() if memory_id in key]
        if "FROM memory_dream_runs" in normalized:
            return [
                {"id": run["id"]}
                for run in self.runs.values()
                if run.get("status") in {"started", "running"}
            ]
        if "FROM memory_dream_snapshots" not in normalized:
            return []
        run_id = str(params[0])
        rows = [row for row in self.snapshots if row["run_id"] == run_id and row["applied"]]
        return sorted(rows, key=lambda row: row["id"], reverse=True)

    def _snapshot(self, snapshot_id: int) -> dict[str, Any]:
        return next(row for row in self.snapshots if row["id"] == snapshot_id)


def test_update_run_rejects_unknown_fields() -> None:
    db = _FakeDreamDB()
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    with pytest.raises(ValueError, match="unknown_column"):
        store.update_run(run_id, unknown_column="bad")


def test_mark_interrupted_runs_reconciles_non_terminal_runs() -> None:
    db = _FakeDreamDB()
    store = _dream_store(db)
    running = store.create_run(project_id="proj-1", dry_run=False, options={})
    started = store.create_run(project_id="proj-1", dry_run=False, options={})
    db.runs[started]["status"] = "started"
    completed = store.create_run(project_id="proj-1", dry_run=False, options={})
    store.update_run(completed, status="completed")

    interrupted = store.mark_interrupted_runs()

    assert set(interrupted) == {running, started}
    assert db.runs[running]["status"] == "interrupted"
    assert db.runs[started]["status"] == "interrupted"
    assert db.runs[running]["error"] == INTERRUPTED_RESTART_ERROR
    assert db.runs[running]["completed_at"] is not None
    # A run that already reached a terminal state is left untouched.
    assert db.runs[completed]["status"] == "completed"


def test_mark_interrupted_runs_is_noop_without_orphans() -> None:
    db = _FakeDreamDB()
    store = _dream_store(db)
    completed = store.create_run(project_id="proj-1", dry_run=False, options={})
    store.update_run(completed, status="completed")

    assert store.mark_interrupted_runs() == []
    assert db.runs[completed]["status"] == "completed"


def test_reconcile_interrupted_dream_runs_uses_manager_db() -> None:
    from types import SimpleNamespace

    from gobby.memory.dream.cron import reconcile_interrupted_dream_runs

    db = _FakeDreamDB()
    store = _dream_store(db)
    running = store.create_run(project_id="proj-1", dry_run=False, options={})

    result = reconcile_interrupted_dream_runs(SimpleNamespace(db=db))

    assert result == [running]
    assert db.runs[running]["status"] == "interrupted"


def test_restore_memory_row_rejects_incomplete_snapshot() -> None:
    store = _dream_store(_FakeDreamDB())
    row = _row("memory-1", "content")
    row.pop("updated_at")

    with pytest.raises(ValueError, match="missing columns: updated_at"):
        store.restore_memory_row(row)


def test_snapshots_serialize_datetime_rows_to_iso_strings() -> None:
    """Regression: psycopg dict_row returns TIMESTAMPTZ columns as datetimes.

    Snapshot payloads are raw memory rows; ``_json`` must convert datetime
    values to ISO strings instead of raising ``TypeError`` (which aborted
    every mutating dream sweep before any row was touched).
    """
    db = _FakeDreamDB()
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    before = _row("memory-1", "content")
    after = dict(before, deleted_at=datetime(2025, 1, 2, tzinfo=UTC), dream_action="delete")

    snapshot_id = store.insert_snapshot(
        run_id=run_id,
        memory_id="memory-1",
        action="delete",
        before_data=before,
    )
    store.complete_snapshot(snapshot_id, after_data=after)
    second_snapshot_id = store.insert_snapshot(
        run_id=run_id,
        memory_id="memory-2",
        action="review",
        before_data=before,
    )
    store.complete_snapshot(second_snapshot_id, after_data=after)

    snapshots = store.list_snapshots(run_id)
    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert snapshot["before_data"]["created_at"] == "2025-01-01T00:00:00+00:00"
        assert snapshot["before_data"]["deleted_at"] is None
        assert snapshot["after_data"]["deleted_at"] == "2025-01-02T00:00:00+00:00"


class _FakeMemoryManager:
    def __init__(self, db: _FakeDreamDB) -> None:
        self.db = db
        self.vector_ids = set(db.memories)
        self.graph_ids = set(db.memories)
        self.delete_memory = AsyncMock(side_effect=self._delete)
        self.notify_memory_changed = MagicMock()
        self.sync_memory_scope_indices = AsyncMock(return_value=[])
        self.restore_memory_indices = AsyncMock(side_effect=self._restore_indices)
        self.reconcile_stores = AsyncMock(return_value={"success": True})

    async def _delete(self, memory_id: str) -> bool:
        deleted = self.db.memories.pop(memory_id, None) is not None
        if deleted:
            self.db.crossrefs = {
                key: row for key, row in self.db.crossrefs.items() if memory_id not in key
            }
            self.vector_ids.discard(memory_id)
            self.graph_ids.discard(memory_id)
        return deleted

    async def _restore_indices(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
        memory_type: str,
        *,
        notify_changed: bool = True,
    ) -> bool:
        del content, project_id, is_global, memory_type, notify_changed
        self.vector_ids.add(memory_id)
        self.graph_ids.add(memory_id)
        return True


class _FakeSweepManager:
    """Stateful manager exercising the streaming sweep cooldown query in memory."""

    def __init__(self, db: _FakeDreamDB) -> None:
        self.db = db
        self.notify_memory_changed = MagicMock()
        self.restore_memory_indices = AsyncMock(return_value=True)
        self.sync_memory_scope_indices = AsyncMock(return_value=[])

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[Any]:
        matches: list[dict[str, Any]] = []
        for row in self.db.memories.values():
            if row.get("deleted_at") is not None:
                continue
            last_dreamed = row.get("last_dreamed_at")
            if last_dreamed is not None and last_dreamed >= redream_cutoff:
                continue
            if scope.kind is MemoryScopeKind.GLOBAL_ONLY:
                if not row["is_global"]:
                    continue
            elif scope.kind is MemoryScopeKind.PROJECT_ONLY:
                in_scope = row["project_id"] == scope.project_id and not row["is_global"]
                if not in_scope:
                    continue
            elif scope.kind is MemoryScopeKind.PROJECT_VISIBLE:
                in_scope = (row["project_id"] == scope.project_id and not row["is_global"]) or row[
                    "is_global"
                ]
                if not in_scope:
                    continue
            if memory_type is not None and row.get("memory_type") != memory_type:
                continue
            matches.append(row)
        matches.sort(
            key=lambda r: (
                r.get("last_dreamed_at") or "",
                r.get("updated_at") or "",
                r["id"],
            )
        )
        return [SimpleNamespace(**row) for row in matches[:limit]]

    def list_dream_candidate_ids(
        self,
        *,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        rows = self.list_dream_candidates(
            limit=len(self.db.memories),
            redream_cutoff=redream_cutoff,
            scope=scope,
            memory_type=memory_type,
        )
        ids = [str(row.id) for row in rows]
        return ids if limit is None else ids[:limit]

    def get_memories(self, memory_ids: list[str], scope: MemoryScope) -> list[Any]:
        rows: list[Any] = []
        for memory_id in memory_ids:
            row = self.db.memories.get(memory_id)
            if row is None or row.get("deleted_at") is not None:
                continue
            if scope.kind is MemoryScopeKind.GLOBAL_ONLY and not row["is_global"]:
                continue
            if scope.kind is MemoryScopeKind.PROJECT_ONLY:
                if row["project_id"] != scope.project_id or row["is_global"]:
                    continue
            if scope.kind is MemoryScopeKind.PROJECT_VISIBLE:
                visible = row["is_global"] or (
                    row["project_id"] == scope.project_id and not row["is_global"]
                )
                if not visible:
                    continue
            rows.append(SimpleNamespace(**row))
        return rows

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        return {}

    async def delete_memory(self, memory_id: str) -> bool:
        return self.db.memories.pop(memory_id, None) is not None


def _sweep_config(
    *,
    unit_size: int = 2,
    dry_run_max_candidates: int = 1000,
    redream_after_hours: int = 20,
    related_evidence_enabled: bool = False,
) -> MemoryDreamConfig:
    return MemoryDreamConfig(
        enabled=True,
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
        reconcile_after_apply=False,
        reconcile_after_revert=False,
        # Work units select planner_batch_size candidates per page.
        planner_batch_size=unit_size,
        dry_run_max_candidates=dry_run_max_candidates,
        redream_after_hours=redream_after_hours,
        include_global_memories=True,
        related_evidence_enabled=related_evidence_enabled,
        related_evidence_top_k=3,
        related_evidence_fetch_limit=10,
    )


_EMPTY_PLAN: dict[str, Any] = {"actions": [], "planner_errors": []}


def _keep_all_planner() -> Any:
    """Patch the unit planner with an empty plan: validation degrades every
    candidate to a visible keep, so sweeps stamp and drain like the old path."""
    return patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(return_value=_EMPTY_PLAN),
    )


def _delete_all_planner() -> Any:
    """Patch the unit planner to delete every candidate, forcing snapshots."""

    async def _plan(*, candidates: list[Any], **_: Any) -> dict[str, Any]:
        return {
            "actions": [
                {
                    "action": "delete",
                    "memory_id": candidate.id,
                    "confidence": 1.0,
                    "reason": "test delete",
                }
                for candidate in candidates
            ],
            "planner_errors": [],
        }

    return patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(side_effect=_plan),
    )


@pytest.mark.asyncio
async def test_streaming_sweep_drains_and_immediate_rerun_is_noop() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(5)}
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=2),
        llm_service=MagicMock(),
    )

    with _keep_all_planner():
        result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is True
    summary = result["run"]["summary"]
    assert summary["candidates_reviewed"] == 5
    assert summary["pages"] == 3  # ceil(5 / 2)
    assert summary["mutations"] == 0
    assert summary["actions"].get("keep") == 5
    assert all(row["last_dreamed_at"] is not None for row in db.memories.values())

    with _keep_all_planner():
        rerun = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    # Everything was just stamped inside the cooldown window, so the re-run is a no-op.
    assert rerun["success"] is True
    assert rerun["run"]["summary"]["candidates_reviewed"] == 0
    assert rerun["run"]["summary"]["pages"] == 0


@pytest.mark.asyncio
async def test_sweep_snapshot_totals_are_per_unit_not_cumulative() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(5)}
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=2),
        llm_service=MagicMock(),
    )

    with _delete_all_planner():
        result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is True
    summary = result["run"]["summary"]
    applied = sum(1 for row in db.snapshots if row["applied"])
    assert summary["mutations"] == 5
    assert applied == 5
    # Regression: unit summaries carry per-unit snapshot deltas; summing the
    # run-cumulative count across the 3 units would report 11 here, not 5.
    assert summary["snapshots"] == applied


@pytest.mark.asyncio
async def test_apply_skips_audit_keep_marker_without_memory_id() -> None:
    """An id-less keep is the validator's audit marker, not a failed action."""
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=_as_dream_manager(manager),
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="keep", reason="unknown candidate id", confidence=0.0)],
        candidates=[],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert summary["errors"] == 0
    assert summary["error_details"] == []
    assert summary["mutations"] == 0
    assert summary["actions"] == {"keep": 1}
    assert db.snapshots == []


@pytest.mark.asyncio
async def test_work_units_cap_selection_at_25_candidates() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i:02d}": _row(f"m{i:02d}", f"content {i}") for i in range(30)}

    class _RecordingManager(_FakeSweepManager):
        def __init__(self, db: _FakeDreamDB) -> None:
            super().__init__(db)
            self.limits: list[int] = []

        def list_dream_candidates(self, *, limit: int, **kwargs: Any) -> list[Any]:
            self.limits.append(limit)
            return super().list_dream_candidates(limit=limit, **kwargs)

    manager = _RecordingManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        # A batch size above the ceiling must still select 25-candidate units.
        dream_config=_sweep_config(unit_size=100),
        llm_service=MagicMock(),
    )

    with _keep_all_planner():
        result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is True
    assert set(manager.limits) == {WORK_UNIT_MAX_CANDIDATES}
    summary = result["run"]["summary"]
    assert summary["candidates_reviewed"] == 30
    assert summary["pages"] == 2  # 25 + 5
    checkpoint = result["run"]["checkpoint"]
    assert checkpoint["phase"] == "sweep"
    assert checkpoint["stop_reason"] == "drained"
    assert checkpoint["completed"] == 30


@pytest.mark.asyncio
async def test_planner_dependency_failure_leaves_candidates_due() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(3)}
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10),
        llm_service=MagicMock(),
    )
    failed_plan = {"actions": [], "planner_errors": ["provider fallback exhausted"]}

    with patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(return_value=failed_plan),
    ):
        result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is False
    assert "memory dream planner failed" in result["error"]
    assert "provider fallback exhausted" in result["error"]
    run = result["run"]
    assert run["status"] == "failed"
    # No implicit keeps, no snapshots, no cursor stamps: every candidate stays due.
    assert db.snapshots == []
    assert all(row["last_dreamed_at"] is None for row in db.memories.values())
    checkpoint = run["checkpoint"]
    assert checkpoint["stop_reason"] == "dependency_failure"
    assert "provider fallback exhausted" in checkpoint["last_dependency_failure"]


@pytest.mark.asyncio
async def test_planner_absence_is_typed_dependency_failure() -> None:
    db = _FakeDreamDB()
    db.memories = {"m0": _row("m0", "content 0")}
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10),
        llm_service=None,
    )

    result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is False
    assert "planner unavailable" in result["error"]
    assert result["run"]["status"] == "failed"
    assert db.memories["m0"]["last_dreamed_at"] is None
    assert db.snapshots == []


@pytest.mark.asyncio
async def test_skip_consolidation_records_inventory_only() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(3)}
    manager = _FakeSweepManager(db)
    llm_service = MagicMock()
    llm_service.call_json_feature = AsyncMock()
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10, related_evidence_enabled=True),
        llm_service=llm_service,
    )

    with (
        patch("gobby.memory.dream.orchestrator.build_raw_plan", new_callable=AsyncMock) as planner,
        patch(
            "gobby.memory.dream.orchestrator.apply_dream_plan", new_callable=AsyncMock
        ) as apply_plan,
        patch(
            "gobby.memory.dream.orchestrator.gather_related_evidence", new_callable=AsyncMock
        ) as gather,
    ):
        result = await service.run(
            DreamRunOptions(dry_run=False, skip_consolidation=True, project_id="proj-1")
        )

    assert result["success"] is True
    planner.assert_not_awaited()
    apply_plan.assert_not_awaited()
    gather.assert_not_awaited()
    llm_service.call_json_feature.assert_not_awaited()
    run = result["run"]
    assert run["status"] == "completed"
    assert run["summary"]["skip_consolidation"] is True
    assert run["summary"]["candidates_eligible"] == 3
    assert run["summary"]["mutations"] == 0
    assert run["plan"]["candidate_count"] == 3
    assert sorted(run["plan"]["candidate_ids"]) == ["m0", "m1", "m2"]
    # Zero snapshot, mutation, or cursor writes: inventory candidates remain due.
    assert db.snapshots == []
    assert all(row["last_dreamed_at"] is None for row in db.memories.values())
    remaining = manager.list_dream_candidate_ids(
        redream_cutoff=datetime.now(UTC).isoformat(),
        scope=MemoryScope.project_visible("proj-1"),
    )
    assert sorted(remaining) == ["m0", "m1", "m2"]


@pytest.mark.asyncio
async def test_streaming_sweep_soft_hides_obsolete_and_keeps_current() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "obsolete": _row("obsolete", "Graph backend is Neo4j"),
        "current": _row("current", "Graph backend is FalkorDB"),
    }
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10),
        llm_service=MagicMock(),
    )
    canned = {
        "actions": [
            {"action": "delete", "memory_id": "obsolete", "confidence": 1.0, "reason": "retired"}
        ],
        "planner_errors": [],
    }

    with patch("gobby.memory.dream.orchestrator.build_raw_plan", AsyncMock(return_value=canned)):
        result = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))

    assert result["success"] is True
    assert db.memories["obsolete"]["deleted_at"] is not None
    assert db.memories["obsolete"]["dream_action"] == "delete"
    assert db.memories["current"]["deleted_at"] is None
    assert db.memories["current"]["last_dreamed_at"] is not None
    summary = result["run"]["summary"]
    assert summary["mutations"] == 1
    assert summary["actions"].get("delete") == 1
    assert summary["actions"].get("keep") == 1


@pytest.mark.asyncio
async def test_dry_run_previews_without_writing_or_stamping() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "obsolete": _row("obsolete", "Graph backend is Neo4j"),
        "current": _row("current", "Graph backend is FalkorDB"),
    }
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10),
        llm_service=MagicMock(),
    )
    canned = {
        "actions": [{"action": "delete", "memory_id": "obsolete", "confidence": 1.0}],
        "planner_errors": [],
    }

    with patch("gobby.memory.dream.orchestrator.build_raw_plan", AsyncMock(return_value=canned)):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    assert result["success"] is True
    # Dry-run pagination performs no memory, snapshot, or stamp writes.
    assert all(row["deleted_at"] is None for row in db.memories.values())
    assert all(row["last_dreamed_at"] is None for row in db.memories.values())
    summary = result["run"]["summary"]
    assert summary["mutations"] == 0
    assert summary["candidates_reviewed"] == 2
    assert summary["pages"] == 1
    assert summary.get("planned_actions")


@pytest.mark.asyncio
async def test_dry_run_persists_unit_and_terminal_checkpoints() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m-{i:02d}": _row(f"m-{i:02d}", f"content {i}") for i in range(5)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=2),
        llm_service=MagicMock(),
    )
    recorded: list[dict[str, Any]] = []
    original = DreamSweepOrchestrator._persist_checkpoint

    async def recording(self: DreamSweepOrchestrator, checkpoint: DreamCheckpoint) -> None:
        recorded.append(checkpoint.to_dict())
        await original(self, checkpoint)

    with (
        _keep_all_planner(),
        patch.object(DreamSweepOrchestrator, "_persist_checkpoint", recording),
    ):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    assert result["success"] is True
    # One durable checkpoint per 2-candidate unit, then the terminal write.
    assert [entry["completed"] for entry in recorded] == [2, 4, 5, 5]
    assert [entry["batch_number"] for entry in recorded] == [1, 2, 3, 3]
    assert recorded[-1]["stop_reason"] == "drained"
    checkpoint = result["run"]["checkpoint"]
    assert checkpoint["phase"] == "sweep"
    assert checkpoint["stop_reason"] == "drained"
    assert checkpoint["completed"] == 5


@pytest.mark.asyncio
async def test_dry_run_dependency_failure_persists_checkpoint() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(3)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=10),
        llm_service=MagicMock(),
    )
    failed_plan = {"actions": [], "planner_errors": ["provider fallback exhausted"]}

    with patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(return_value=failed_plan),
    ):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    assert result["success"] is False
    run = result["run"]
    assert run["status"] == "failed"
    checkpoint = run["checkpoint"]
    assert checkpoint["stop_reason"] == "dependency_failure"
    assert "provider fallback exhausted" in checkpoint["last_dependency_failure"]


@pytest.mark.asyncio
async def test_dry_run_window_exhaustion_records_partial() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m-{i:02d}": _row(f"m-{i:02d}", f"content {i}") for i in range(4)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=2),
        llm_service=MagicMock(),
    )
    # Admission stays open for the first unit, then the window closes.
    window = iter([True, False])

    with (
        _keep_all_planner(),
        patch.object(DreamSweepOrchestrator, "window_open", lambda self: next(window)),
    ):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    assert result["success"] is True
    run = result["run"]
    assert run["status"] == "partial"
    assert run["summary"]["candidates_reviewed"] == 2
    checkpoint = run["checkpoint"]
    assert checkpoint["stop_reason"] == "window_exhausted"
    assert checkpoint["completed"] == 2


async def test_sweeps_attach_related_evidence() -> None:
    live_db = _FakeDreamDB()
    live_db.memories = {f"live-{i}": _row(f"live-{i}", f"live {i}") for i in range(5)}
    dry_db = _FakeDreamDB()
    dry_db.memories = {f"dry-{i}": _row(f"dry-{i}", f"dry {i}") for i in range(5)}
    live_session = MagicMock()
    live_session.aclose = AsyncMock()
    dry_session = MagicMock()
    dry_session.aclose = AsyncMock()

    async def attach(candidates: list[DreamCandidate], **_kwargs: Any) -> list[DreamCandidate]:
        return candidates

    with (
        patch(
            "gobby.memory.dream.service.RelatedEvidenceSession",
            side_effect=[live_session, dry_session],
        ),
        patch(
            "gobby.memory.dream.orchestrator.gather_related_evidence",
            AsyncMock(side_effect=attach),
        ) as gather,
        _keep_all_planner(),
    ):
        live_service = MemoryDreamService(
            memory_manager=_as_dream_manager(_FakeSweepManager(live_db)),
            dream_config=_sweep_config(unit_size=2, related_evidence_enabled=True),
            llm_service=MagicMock(),
        )
        dry_service = MemoryDreamService(
            memory_manager=_as_dream_manager(_FakeSweepManager(dry_db)),
            dream_config=_sweep_config(unit_size=2, related_evidence_enabled=True),
            llm_service=MagicMock(),
        )
        await live_service.run(DreamRunOptions(project_id="proj-1"))
        await dry_service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    assert gather.await_count == 6
    assert all(call.kwargs["session"] is live_session for call in gather.await_args_list[:3])
    assert all(call.kwargs["session"] is dry_session for call in gather.await_args_list[3:])
    live_session.aclose.assert_awaited_once()
    dry_session.aclose.assert_awaited_once()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_sweeps_skip_related_evidence_when_disabled(dry_run: bool) -> None:
    db = _FakeDreamDB()
    db.memories = {f"m-{i}": _row(f"m-{i}", f"content {i}") for i in range(3)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=2, related_evidence_enabled=False),
        llm_service=MagicMock(),
    )

    with (
        patch(
            "gobby.memory.dream.orchestrator.gather_related_evidence", new_callable=AsyncMock
        ) as gather,
        _keep_all_planner(),
    ):
        result = await service.run(DreamRunOptions(dry_run=dry_run, project_id="proj-1"))

    assert result["success"] is True
    assert result["run"]["summary"]["candidates_reviewed"] == 3
    gather.assert_not_awaited()


async def test_dry_run_full_coverage_pagination() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m-{i:02d}": _row(f"m-{i:02d}", f"content {i}") for i in range(55)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(unit_size=10, related_evidence_enabled=False),
        llm_service=MagicMock(),
    )

    async def plan_page(**kwargs: Any) -> dict[str, Any]:
        return {
            "actions": [
                {"action": "keep", "memory_id": candidate.id, "confidence": 1.0}
                for candidate in kwargs["candidates"]
            ],
            "planner_errors": [],
        }

    with patch("gobby.memory.dream.orchestrator.build_raw_plan", side_effect=plan_page):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    run = result["run"]
    actions = run["plan"]["actions"]
    assert run["summary"]["candidates_reviewed"] == 55
    assert run["summary"]["pages"] == 6
    assert run["summary"]["planned_action_count"] == 55
    assert len(actions) == MAX_ACTION_SAMPLE
    assert run["summary"]["candidates_truncated"] is False
    assert all(row["last_dreamed_at"] is None for row in db.memories.values())

    with patch("gobby.cli.memory.dream._request", return_value={"success": True, "run": run}):
        output = CliRunner().invoke(memory_dream, ["status", run["id"]]).output

    assert actions[-1]["memory_id"] in output


async def test_dry_run_candidate_limit_reports_truncation() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m-{i:02d}": _row(f"m-{i:02d}", f"content {i}") for i in range(30)}
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(
            unit_size=10,
            dry_run_max_candidates=25,
            related_evidence_enabled=False,
        ),
        llm_service=MagicMock(),
    )

    async def plan_page(**kwargs: Any) -> dict[str, Any]:
        return {
            "actions": [
                {"action": "keep", "memory_id": candidate.id, "confidence": 1.0}
                for candidate in kwargs["candidates"]
            ],
            "planner_errors": [],
        }

    with patch("gobby.memory.dream.orchestrator.build_raw_plan", side_effect=plan_page):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    summary = result["run"]["summary"]
    assert summary["candidates_reviewed"] == 25
    assert summary["candidate_limit"] == 25
    assert summary["candidates_truncated"] is True
    assert summary["planned_action_count"] == 25


async def test_dry_run_snapshot_interleaving() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m-{i}": _row(f"m-{i}", f"content {i}") for i in range(5)}
    for row in db.memories.values():
        row["last_dreamed_at"] = "2025-01-01T00:00:00+00:00"
    manager = _FakeSweepManager(db)
    snapshot_ids = manager.list_dream_candidate_ids(
        redream_cutoff="2026-01-01T00:00:00+00:00",
        scope=MemoryScope.project_visible("proj-1"),
    )
    deleted_id = snapshot_ids[-1]
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=2, related_evidence_enabled=False),
        llm_service=MagicMock(),
    )
    page_count = 0

    async def plan_page(**kwargs: Any) -> dict[str, Any]:
        nonlocal page_count
        page_count += 1
        if page_count == 1:
            db.memories[snapshot_ids[2]]["last_dreamed_at"] = None
            db.memories[snapshot_ids[3]]["updated_at"] = datetime.now(UTC)
            del db.memories[deleted_id]
        return {
            "actions": [
                {"action": "keep", "memory_id": candidate.id, "confidence": 1.0}
                for candidate in kwargs["candidates"]
            ],
            "planner_errors": [],
        }

    with (
        patch("gobby.memory.dream.orchestrator.build_raw_plan", side_effect=plan_page),
        patch("gobby.memory.dream.candidates.logger.info") as log_info,
    ):
        result = await service.run(DreamRunOptions(dry_run=True, project_id="proj-1"))

    reviewed_ids = [action["memory_id"] for action in result["run"]["plan"]["actions"]]
    assert reviewed_ids.count(snapshot_ids[2]) == 1
    assert reviewed_ids.count(snapshot_ids[3]) == 1
    assert set(reviewed_ids) == set(snapshot_ids) - {deleted_id}
    assert deleted_id in str(log_info.call_args_list)


async def test_global_only_run_persists_null_scope_and_selects_only_global() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "global": _row("global", "global content"),
        "project": _row("project", "project content"),
    }
    db.memories["global"]["is_global"] = True
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=10),
        llm_service=MagicMock(),
    )

    with _keep_all_planner():
        result = await service.run(DreamRunOptions(dry_run=False, global_only=True))

    assert result["success"] is True
    run = result["run"]
    assert run["project_id"] is None
    assert run["options"]["global_only"] is True
    assert run["summary"]["candidates_reviewed"] == 1
    assert db.memories["global"]["last_dreamed_at"] is not None
    assert db.memories["project"]["last_dreamed_at"] is None


def test_global_only_rejects_project_id() -> None:
    with pytest.raises(ValueError, match="global_only and project_id"):
        DreamRunOptions(global_only=True, project_id="proj-1")


def test_build_current_truth_digest_includes_canonical_facts() -> None:
    digest = build_current_truth_digest()

    assert "FalkorDB" in digest
    assert "PostgreSQL" in digest
    assert "Neo4j" in digest  # names the retired backend the planner should flag


def test_build_current_truth_digest_allowlists_config_and_redacts_secrets() -> None:
    config = SimpleNamespace(
        hub_backend="postgres",
        daemon_port=60887,
        bind_host="127.0.0.1",
        database_url="postgresql://user:supersecret@host/db",
    )

    digest = build_current_truth_digest(config, max_chars=5000)

    assert "postgres" in digest
    assert "60887" in digest
    assert "127.0.0.1" in digest
    # database_url is not on the allowlist and must never reach the prompt.
    assert "supersecret" not in digest
    assert "database_url" not in digest


def test_build_current_truth_digest_bounds_length() -> None:
    digest = build_current_truth_digest(max_chars=80)

    assert len(digest) <= 80


def test_build_project_truth_digest_renders_authoritative_stack(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _write_truth_digest(repo_path, _complete_digest_payload(service="React UI"))

    digest = build_project_truth_digest(str(repo_path))

    assert "Repository summary: A small web frontend with generated docs." in digest
    assert "Current infrastructure stack (authoritative - complete current set):" in digest
    assert "React UI (frontend)" in digest
    assert "adapter: src/app.tsx:10" in digest
    assert "Key paths: React UI: src/app.tsx:10" in digest
    assert "Knowledge graph backend: FalkorDB" not in digest


def test_build_project_truth_digest_renders_partial_and_empty_without_platform_facts(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    _write_truth_digest(
        repo_path,
        {
            "schema_version": 1,
            "repo_summary": "A non-Rust repo.",
            "stack_authority": "partial",
            "stack": [],
        },
    )

    digest = build_project_truth_digest(str(repo_path))

    assert "Known infrastructure (partial - do NOT infer staleness from absence):" in digest
    assert "none listed" in digest
    assert "Knowledge graph backend: FalkorDB" not in digest


def test_build_project_truth_digest_missing_or_invalid_returns_empty(tmp_path: Path) -> None:
    assert build_project_truth_digest(str(tmp_path / "missing")) == ""

    repo_path = tmp_path / "repo"
    marker = repo_path / "wiki" / "_gwiki" / "scope.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}\n", encoding="utf-8")
    digest_path = repo_path / "wiki" / "_meta" / "truth_digest.json"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text("{invalid", encoding="utf-8")

    assert build_project_truth_digest(str(repo_path)) == ""


def test_build_project_truth_digest_matches_real_gobby_cli_artifact_shape(
    tmp_path: Path,
) -> None:
    """Pin the consumer to the REAL gobby-cli ``truth_digest.json`` field names.

    The fixture is a trimmed copy of a real vault ``_meta/truth_digest.json``
    emitted by the gobby-cli codewiki build (stack reduced to two entries and the
    long ``summary``/``degradation`` strings shortened so every consumed field
    renders inside the digest bound). It deliberately preserves the producer's
    field names verbatim so the cross-repo handshake is CI-enforced: if gobby-cli
    renames a field the fixture carries the new name and the consumer's
    ``in digest`` assertions fail; if the consumer stops reading a field the same
    assertions fail. This catches the field drift that synthetic, co-authored
    fixtures cannot.
    """
    fixture_path = Path(__file__).parent / "fixtures" / "gobby_cli_truth_digest.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    # schema_version is the contract version the consumer is written against; a
    # producer bump is a deliberate signal to re-review the consumer.
    assert payload["schema_version"] == 1

    repo_path = tmp_path / "repo"
    _write_truth_digest(repo_path, payload)

    digest = build_project_truth_digest(str(repo_path))

    # stack_authority -> authoritative lead (not the partial/absence wording).
    assert "Current infrastructure stack (authoritative - complete current set):" in digest
    # repo_summary
    assert f"Repository summary: {payload['repo_summary']}" in digest

    # Every consumed stack[] field renders for every entry. Subscripting the
    # payload means a renamed field in the fixture raises KeyError here, while
    # the membership checks catch a consumer that stops reading the field.
    assert payload["stack"], "fixture must carry at least one stack entry"
    for entry in payload["stack"]:
        assert f"{entry['service']} ({entry['kind']})" in digest  # service + kind
        assert entry["summary"] in digest  # summary
        assert f"adapter: {entry['adapter_module']}" in digest  # adapter_module
        assert f"pulled in by: {', '.join(entry['pulled_in_by'])}" in digest  # pulled_in_by
        assert f"degradation: {entry['degradation']}" in digest  # degradation

    # key_paths
    assert payload["key_paths"], "fixture must carry at least one key path"
    for label, path in payload["key_paths"].items():
        assert f"{label}: {path}" in digest

    # No Gobby platform facts leak into a project sweep.
    assert "Knowledge graph backend: FalkorDB" not in digest


@pytest.mark.asyncio
async def test_build_project_truth_digest_async_matches_sync_render(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _write_truth_digest(repo_path, _complete_digest_payload(service="Async sidecar"))

    assert await build_project_truth_digest_async(str(repo_path)) == build_project_truth_digest(
        str(repo_path)
    )


@pytest.mark.asyncio
async def test_daemon_project_digest_blend_live_path(
    tmp_path: Path,
) -> None:
    current_project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    db = _FakeDreamDB()
    current_repo = tmp_path / "current"
    _write_truth_digest(current_repo, _complete_digest_payload(service="Current repo sidecar"))
    db.projects[current_project_id] = _project_row(current_project_id, current_repo)
    db.memories = {
        "global": {**_row("global", "Global memory"), "is_global": True},
        "current": {**_row("current", "Current project memory"), "project_id": current_project_id},
    }

    global_digest = await _capture_service_truth_digest(
        db,
        DreamRunOptions(dry_run=True, global_only=True),
        current_project_id=current_project_id,
    )
    current_digest = await _capture_service_truth_digest(
        db,
        DreamRunOptions(dry_run=True, project_id=current_project_id),
        current_project_id=current_project_id,
    )

    assert "Knowledge graph backend: FalkorDB" in global_digest
    assert "Knowledge graph backend: FalkorDB" in current_digest
    assert "Current repo sidecar (frontend)" in current_digest
    assert "Current repo sidecar" not in global_digest


@pytest.mark.asyncio
async def test_service_uses_project_truth_only_for_non_daemon_project(tmp_path: Path) -> None:
    # Repo-path resolution goes through LocalProjectManager.get, which returns
    # None for non-uuid project ids, so these must be valid-format UUIDs.
    current_project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    other_project_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    db = _FakeDreamDB()
    current_repo = tmp_path / "current"
    other_repo = tmp_path / "other"
    _write_truth_digest(current_repo, _complete_digest_payload(service="Current sidecar"))
    _write_truth_digest(other_repo, _complete_digest_payload(service="Other sidecar"))
    db.projects[current_project_id] = _project_row(current_project_id, current_repo)
    db.projects[other_project_id] = _project_row(other_project_id, other_repo)
    db.memories = {
        "other": {**_row("other", "Other project memory"), "project_id": other_project_id}
    }

    digest = await _capture_service_truth_digest(
        db,
        DreamRunOptions(dry_run=True, project_id=other_project_id, include_global=False),
        current_project_id=current_project_id,
    )

    assert "Other sidecar (frontend)" in digest
    assert "Current sidecar" not in digest
    assert "Knowledge graph backend: FalkorDB" not in digest


@pytest.mark.asyncio
async def test_service_current_project_id_none_selects_no_daemon_project(tmp_path: Path) -> None:
    # Must be a valid-format UUID so LocalProjectManager.get resolves the repo path.
    project_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    db = _FakeDreamDB()
    repo_path = tmp_path / "repo"
    _write_truth_digest(repo_path, _complete_digest_payload(service="Repo sidecar"))
    db.projects[project_id] = _project_row(project_id, repo_path)
    db.memories = {
        "21000000-0000-4000-8000-000000000005": {
            **_row("21000000-0000-4000-8000-000000000005", "Project memory"),
            "project_id": project_id,
        }
    }

    digest = await _capture_service_truth_digest(
        db,
        DreamRunOptions(dry_run=True, project_id=project_id, include_global=False),
        current_project_id=None,
    )

    assert "Repo sidecar (frontend)" in digest
    assert "Knowledge graph backend: FalkorDB" not in digest


def test_dream_prompt_declares_actions_and_truth_digest() -> None:
    import gobby

    prompt = (Path(gobby.__file__).parent / "install/shared/prompts/memory/dream.md").read_text(
        encoding="utf-8"
    )

    assert "{{ truth_digest }}" in prompt
    assert "{{ min_rescope_confidence }}" in prompt
    for action in ("keep", "delete", "refresh", "review", "promote"):
        assert f"- `{action}`" in prompt
    # merge/supersede are no longer offered as verdicts.
    assert "- `merge`" not in prompt
    assert "- `supersede`" not in prompt
    # obsolete-fact guidance names the canonical infra migrations.
    assert "Neo4j" in prompt
    assert "MySQL" in prompt
    assert "authoritative - complete current set" in prompt
    assert "partial - do NOT infer staleness from absence" in prompt


def test_prompt_contract_evidence_and_access_count(temp_db: HubDatabase) -> None:
    sync_bundled_prompts(temp_db)
    prompt = PromptLoader(db=temp_db).render(
        "memory/dream",
        {
            "candidates": "[]",
            "truth_digest": "current truth",
            "min_action_confidence": 0.7,
            "min_delete_confidence": 0.9,
            "min_rescope_confidence": 0.9,
        },
        strict=True,
    )

    current_truth = prompt.index("## Current truth")
    related_evidence = prompt.index("## Related newer memories")
    rules = prompt.index("## Rules")
    assert current_truth < related_evidence < rules
    assert "`related_newer_memories`" in prompt
    assert "concrete, citable obsolescence signal" in prompt
    assert "cite its `id` in `reason`" in prompt
    assert "Absent or empty `related_newer_memories` is not evidence of currentness" in prompt
    assert (
        "a newer related memory records a decision or state change that contradicts or "
        "supersedes this memory"
    ) in prompt
    assert "High `access_count` is never evidence of correctness" in prompt
    assert "recall frequency measures retrieval, not truth" in prompt
    assert "a wrong-but-popular memory self-reinforces" in prompt
    assert "Only `access_count` at or near zero may corroborate a `delete`" in prompt
    assert (
        "never use high `access_count` to justify `keep` against a concrete obsolescence signal"
    ) in prompt
    assert "does not require low `access_count` when a contradiction signal exists" in prompt


@pytest.mark.asyncio
async def test_full_sweep_ignores_cooldown_and_reviews_all() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(5)}
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=2),
        llm_service=MagicMock(),
    )

    with _keep_all_planner():
        first = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))
    assert first["run"]["summary"]["candidates_reviewed"] == 5

    # A default rerun is a no-op: everything was just stamped inside the cooldown.
    with _keep_all_planner():
        cooldown_rerun = await service.run(DreamRunOptions(dry_run=False, project_id="proj-1"))
    assert cooldown_rerun["run"]["summary"]["candidates_reviewed"] == 0

    # full_sweep bypasses the cooldown and reviews the whole corpus again, draining
    # to completion (the unit loop still terminates via per-unit stamping).
    with _keep_all_planner():
        full = await service.run(
            DreamRunOptions(dry_run=False, project_id="proj-1", full_sweep=True)
        )
    assert full["success"] is True
    assert full["run"]["summary"]["candidates_reviewed"] == 5
    assert full["run"]["summary"]["pages"] == 3  # ceil(5 / 2)
    assert all(row["last_dreamed_at"] is not None for row in db.memories.values())


@pytest.mark.asyncio
async def test_full_sweep_cutoff_is_run_start_not_cooldown_window() -> None:
    db = _FakeDreamDB()
    db.memories = {"m0": _row("m0", "content 0")}

    class _RecordingSweepManager(_FakeSweepManager):
        def __init__(self, db: _FakeDreamDB) -> None:
            super().__init__(db)
            self.cutoffs: list[str] = []

        def list_dream_candidates(self, *, redream_cutoff: str, **kwargs: Any) -> list[Any]:
            self.cutoffs.append(redream_cutoff)
            return super().list_dream_candidates(redream_cutoff=redream_cutoff, **kwargs)

    manager = _RecordingSweepManager(db)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(unit_size=2, redream_after_hours=20),
        llm_service=MagicMock(),
    )

    with _keep_all_planner():
        # cooldown: cutoff = run_start - 20h
        await service.run(DreamRunOptions(project_id="proj-1"))
        normal_cutoff = datetime.fromisoformat(manager.cutoffs[0])
        # full path: cutoff = run_start
        await service.run(DreamRunOptions(project_id="proj-1", full_sweep=True))
    full_cutoff = datetime.fromisoformat(manager.cutoffs[-1])

    # full_sweep anchors the cutoff at run start; the cooldown path subtracts 20h.
    # The full run also fires slightly later, so the gap is ~20h (assert >= 19h to
    # absorb wall-clock jitter between the two runs).
    assert full_cutoff - normal_cutoff >= timedelta(hours=19)


class _FakeDueProjectsManager:
    """Manager exposing only list_dream_scopes (+ a stub db) so the
    run_all_due_projects loop can be tested with a patched per-target run()."""

    def __init__(self, targets: list[str | None]) -> None:
        self.targets = targets
        self.cutoffs: list[str] = []
        self.db = _FakeDreamDB()

    def list_dream_scopes(self, *, redream_cutoff: str) -> list[MemoryScope]:
        self.cutoffs.append(redream_cutoff)
        return [
            MemoryScope.global_only() if target is None else MemoryScope.project_only(target)
            for target in self.targets
        ]

    def mark_global_memories_due(self) -> int:
        return 0

    def mark_project_memories_due(self, project_id: str) -> int:
        return 0


@pytest.mark.asyncio
async def test_run_all_due_projects_loops_targets_with_per_target_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeDueProjectsManager(["proj-a", None, "proj-b"])
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(),
        current_project_id="daemon-proj",
    )
    recorded: list[DreamRunOptions] = []

    async def fake_run(options: DreamRunOptions) -> dict[str, Any]:
        recorded.append(options)
        idx = len(recorded)
        mutations = 2 if options.global_only else 1
        return {
            "success": True,
            "run_id": f"run-{idx}",
            "run": {"id": f"run-{idx}", "summary": {"mutations": mutations}},
        }

    monkeypatch.setattr(service, "_run_nested_target", fake_run)

    result = await service.run_all_due_projects(dry_run=True, memory_type="fact", full_sweep=True)

    # Enumeration runs once; the cutoff comes from redream_after_hours.
    assert len(manager.cutoffs) == 1
    assert result["success"] is True
    assert result["targets"] == 3
    assert result["completed"] == 3
    assert result["failed"] == 0
    assert result["mutations"] == 4  # 1 (proj-a) + 2 (global) + 1 (proj-b)

    # The NULL/global bucket runs global_only; real projects run scoped with the
    # global bucket excluded so it is swept exactly once. Manual flags pass through.
    scopes = [
        (o.project_id, o.global_only, o.include_global, o.dry_run, o.memory_type, o.full_sweep)
        for o in recorded
    ]
    assert scopes == [
        ("proj-a", False, False, True, "fact", True),
        (None, True, None, True, "fact", True),
        ("proj-b", False, False, True, "fact", True),
    ]
    assert [r["project_id"] for r in result["runs"]] == ["proj-a", None, "proj-b"]
    assert [r["run_id"] for r in result["runs"]] == ["run-1", "run-2", "run-3"]


class _FakeScopeOrchestrator:
    """Minimal per-scope orchestrator stand-in for coordinator seam tests."""

    def __init__(self) -> None:
        self.totals = SweepTotals()
        self.stop_reason = "drained"

    async def finalize_sweep(self) -> SweepTotals:
        return self.totals


class _FakeRelatedSession:
    async def aclose(self) -> None:
        return None


def _unit(count: int) -> WorkUnitOutcome:
    return WorkUnitOutcome(
        candidates=[_candidate(f"m-{count}-{index}") for index in range(count)],
        actions=[],
        page_summary={"actions": {}},
        raw_plan_metadata={},
    )


def _coordinator_service(
    targets: list[str | None],
    unit_plan: dict[str, list[Any]],
) -> tuple[MemoryDreamService, list[str]]:
    """Round-robin harness: seam-patched service plus recorded visit order.

    ``unit_plan`` maps scope keys to per-visit steps; an Exception step is
    raised from the unit seam instead of returned. Child run rows are real
    (created against the fake DB), so per-scope statuses are observable.
    """
    manager = _FakeDueProjectsManager(targets)
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(),
    )
    visits: list[str] = []

    async def fake_open(scope: MemoryScope, **_kwargs: Any) -> _ScopeSweep:
        run_id = service.store.create_run(
            project_id=scope.project_id, dry_run=False, options={}, status="started"
        )
        return _ScopeSweep(
            scope=scope,
            options=DreamRunOptions(dry_run=False),
            run_id=run_id,
            orchestrator=cast(Any, _FakeScopeOrchestrator()),
            related_session=cast(Any, _FakeRelatedSession()),
            unit_size=2,
        )

    async def fake_unit(sweep: _ScopeSweep) -> WorkUnitOutcome:
        key = _scope_sweep_key(sweep.scope)
        visits.append(key)
        step = unit_plan[key].pop(0)
        if isinstance(step, Exception):
            raise step
        return cast(WorkUnitOutcome, step)

    _set_method(service, "_open_scope_sweep", fake_open)
    _set_method(service, "_run_scope_unit", fake_unit)
    return service, visits


@pytest.mark.asyncio
async def test_round_robin_gives_each_due_scope_one_unit_per_pass() -> None:
    service, visits = _coordinator_service(
        ["proj-a", "proj-b", None],
        {
            "project:proj-a": [_unit(2), _unit(2), _unit(1)],
            "project:proj-b": [_unit(1)],
            "global": [_unit(2), _unit(1)],
        },
    )

    result = await service.run_all_due_projects()

    # One unit per due scope per pass: proj-a's larger backlog cannot starve
    # proj-b or the global bucket, and drained scopes drop out of later passes.
    assert visits == [
        "project:proj-a",
        "project:proj-b",
        "global",
        "project:proj-a",
        "global",
        "project:proj-a",
    ]
    assert result["success"] is True
    assert result["stop_reason"] == "drained"
    assert result["passes"] == 3
    assert result["completed"] == 3
    assert result["failed"] == 0
    statuses = {run["project_id"]: run["status"] for run in result["runs"]}
    assert statuses == {"proj-a": "completed", "proj-b": "completed", None: "completed"}


@pytest.mark.asyncio
async def test_aggregate_runner_honors_patched_service_scope_lifecycle_seams() -> None:
    service, visits = _coordinator_service(
        ["proj-a"],
        {"project:proj-a": [_unit(1)]},
    )
    patched_open = service._open_scope_sweep
    patched_close = service._close_scope_sweep
    opens: list[str] = []
    closes: list[tuple[str, str, str | None]] = []

    async def tracking_open(
        scope: MemoryScope, *, memory_type: str | None, full_sweep: bool
    ) -> _ScopeSweep:
        opens.append(_scope_sweep_key(scope))
        return await patched_open(scope, memory_type=memory_type, full_sweep=full_sweep)

    async def tracking_close(
        sweep: _ScopeSweep, *, status: str, error: str | None = None
    ) -> dict[str, Any]:
        closes.append((_scope_sweep_key(sweep.scope), status, error))
        return await patched_close(sweep, status=status, error=error)

    _set_method(service, "_open_scope_sweep", tracking_open)
    _set_method(service, "_close_scope_sweep", tracking_close)

    result = await service.run_all_due_projects()

    assert opens == ["project:proj-a"]
    assert visits == ["project:proj-a"]
    assert closes == [("project:proj-a", "completed", None)]
    assert result["completed"] == 1


@pytest.mark.asyncio
async def test_window_exhaustion_stops_new_units_and_records_partial(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, visits = _coordinator_service(
        ["proj-a", "proj-b", None],
        {
            "project:proj-a": [_unit(2), _unit(2)],
            "project:proj-b": [_unit(2)],
            "global": [_unit(2)],
        },
    )
    # Admission stays open for two units, then the window closes.
    checks = iter([True, True, False])
    _set_method(service, "_admission_window_open", lambda _deadline: next(checks))
    run_id = service.store.create_run(project_id=None, dry_run=False, options={})
    caplog.set_level("INFO", logger="gobby.memory.dream.aggregate")

    result = await service.execute_all_due_projects_run(run_id)

    # No new unit starts after the deadline: the global scope never runs.
    assert visits == ["project:proj-a", "project:proj-b"]
    assert result["success"] is True
    assert result["status"] == "partial"
    aggregate = result["aggregate"]
    assert aggregate["stop_reason"] == "window_exhausted"
    assert [entry["status"] for entry in aggregate["runs"]] == ["partial", "partial"]
    run = service.store.get_run(run_id)
    assert run is not None
    assert run["status"] == "partial"
    assert run["summary"]["stop_reason"] == "window_exhausted"
    checkpoint = run["checkpoint"]
    assert checkpoint["phase"] == "coordinator"
    assert checkpoint["stop_reason"] == "window_exhausted"
    # A window-exhausted partial is a normal outcome: INFO only, no warnings.
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == []


@pytest.mark.asyncio
async def test_dependency_failure_stops_coordinator_with_single_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = DreamDependencyError(
        "memory dream planner failed: provider fallback exhausted after 3 attempts"
    )
    service, visits = _coordinator_service(
        ["proj-a", "proj-b", None],
        {
            "project:proj-a": [_unit(2), failure],
            "project:proj-b": [_unit(1)],
            "global": [_unit(2), _unit(1)],
        },
    )
    caplog.set_level("INFO", logger="gobby.memory.dream.aggregate")

    result = await service.run_all_due_projects()

    # Pass 1 visits every scope; proj-a's second unit fails and stops the
    # coordinator before the global scope gets its second unit.
    assert visits == ["project:proj-a", "project:proj-b", "global", "project:proj-a"]
    assert result["success"] is True
    assert result["stop_reason"] == "dependency_failure"
    assert "provider fallback exhausted" in result["dependency_failure"]
    entries = {run["project_id"]: run for run in result["runs"]}
    assert entries["proj-a"]["status"] == "failed"
    assert "provider fallback exhausted" in entries["proj-a"]["error"]
    assert entries["proj-b"]["status"] == "completed"
    # The cut-off global scope keeps its completed unit and records partial.
    assert entries[None]["status"] == "partial"
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "provider fallback exhausted" in message
    assert "remain due" in message


@pytest.mark.asyncio
async def test_run_all_due_projects_isolates_target_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _visits = _coordinator_service(
        ["proj-ok", "proj-bad", None],
        {
            "project:proj-ok": [_unit(1)],
            "project:proj-bad": [RuntimeError("boom")],
            "global": [_unit(1)],
        },
    )
    caplog.set_level("ERROR", logger="gobby.memory.dream.aggregate")

    result = await service.run_all_due_projects()

    assert result["success"] is True  # one structural failure does not fail the batch
    assert result["completed"] == 2
    assert result["failed"] == 1
    bad = next(r for r in result["runs"] if r["project_id"] == "proj-bad")
    assert bad["success"] is False
    assert "boom" in bad["error"]
    assert "project_id=proj-bad" in caplog.text


@pytest.mark.asyncio
async def test_run_all_due_projects_all_failed_marks_aggregate_failed() -> None:
    service, _visits = _coordinator_service(
        ["proj-bad", None],
        {
            "project:proj-bad": [RuntimeError("nope")],
            "global": [RuntimeError("nope")],
        },
    )

    result = await service.run_all_due_projects()

    assert result["success"] is False
    assert result["completed"] == 0
    assert result["failed"] == 2


@pytest.mark.asyncio
async def test_run_all_due_projects_disabled_returns_empty_aggregate_without_enumerating() -> None:
    manager = _FakeDueProjectsManager(["proj-a"])
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=MemoryDreamConfig(enabled=False),
    )

    result = await service.run_all_due_projects()

    assert result["success"] is False
    assert result["error"] == "memory dream is disabled"
    assert result["targets"] == 0
    assert manager.cutoffs == []  # never enumerated when disabled


@pytest.mark.asyncio
async def test_truth_change_trigger_rejudges_cooled_memory_on_digest_change(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """A codewiki digest change clears the cooldown for a cooled project.

    The whole point of the truth-change trigger: when a project's codewiki
    ``truth_digest.json`` changes, its memories must be re-judged on the next
    sweep **even inside the cooldown window**. A memory dreamed moments ago is
    firmly cooled (a normal cooldown-throttled sweep would skip it), yet a digest
    change must make it due again; an unchanged digest must leave it cooled.
    """
    pm = LocalProjectManager(temp_db)
    repo = tmp_path / "repo"
    project = pm.create(name="truth-trigger-proj", repo_path=str(repo))
    manager = LocalMemoryManager(temp_db)
    service = MemoryDreamService(
        memory_manager=cast(MemoryDreamManagerProtocol, manager),
        dream_config=_sweep_config(),
    )

    just_now = datetime.now(UTC).isoformat()
    cooldown_cutoff = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    memory = manager.create_memory(content="project stack fact", project_id=project.id)

    def recool() -> None:
        """Stamp the memory as dreamed now, so it sits inside the cooldown."""
        manager.mark_dreamed(memory.id, when=just_now)

    # First observation records the baseline hash (and clears the cooldown once
    # on first sight). Re-cool afterwards so the project is genuinely not due.
    _write_truth_digest(repo, _complete_digest_payload(service="PostgreSQL hub"))
    await service._apply_truth_change_triggers()
    recool()
    assert manager.get_memory(memory.id).last_dreamed_at is not None
    assert MemoryScope.project_only(project.id) not in manager.list_dream_scopes(
        redream_cutoff=cooldown_cutoff
    )

    # Unchanged digest: the trigger is a no-op, the memory stays cooled.
    await service._apply_truth_change_triggers()
    assert manager.get_memory(memory.id).last_dreamed_at is not None
    assert MemoryScope.project_only(project.id) not in manager.list_dream_scopes(
        redream_cutoff=cooldown_cutoff
    )

    # Changed digest: the cooldown is cleared, so the cooled memory is due again
    # and would be swept on the next run despite still being inside the window.
    _write_truth_digest(repo, _complete_digest_payload(service="FalkorDB graph"))
    await service._apply_truth_change_triggers()
    assert manager.get_memory(memory.id).last_dreamed_at is None
    assert MemoryScope.project_only(project.id) in manager.list_dream_scopes(
        redream_cutoff=cooldown_cutoff
    )


@pytest.mark.asyncio
async def test_platform_truth_change_rejudges_global_and_current_project_memories(
    temp_db: HubDatabase,
) -> None:
    pm = LocalProjectManager(temp_db)
    current_project = pm.create(name="current-project", repo_path=None)
    other_project = pm.create(name="other-project", repo_path=None)
    manager = LocalMemoryManager(temp_db)
    service = MemoryDreamService(
        memory_manager=cast(MemoryDreamManagerProtocol, manager),
        dream_config=_sweep_config(),
        current_project_id=current_project.id,
    )

    just_now = datetime.now(UTC).isoformat()
    global_memory = manager.create_memory(
        content="global platform fact",
        project_id=PERSONAL_PROJECT_ID,
        is_global=True,
    )
    current_memory = manager.create_memory(
        content="current platform fact", project_id=current_project.id
    )
    other_memory = manager.create_memory(content="other project fact", project_id=other_project.id)
    for memory in (global_memory, current_memory, other_memory):
        manager.mark_dreamed(memory.id, when=just_now)

    await service._apply_truth_change_triggers()

    assert manager.get_memory(global_memory.id).last_dreamed_at is None
    assert manager.get_memory(current_memory.id).last_dreamed_at is None
    assert manager.get_memory(other_memory.id).last_dreamed_at is not None
    assert service.store.get_truth_digest_hash(PLATFORM_TRUTH_SCOPE) is not None

    manager.mark_dreamed(global_memory.id, when=just_now)
    manager.mark_dreamed(current_memory.id, when=just_now)

    await service._apply_truth_change_triggers()

    assert manager.get_memory(global_memory.id).last_dreamed_at is not None
    assert manager.get_memory(current_memory.id).last_dreamed_at is not None


async def test_build_truth_digest_requires_explicit_scope() -> None:
    db = _FakeDreamDB()
    service = MemoryDreamService(
        memory_manager=_as_dream_manager(_FakeSweepManager(db)),
        dream_config=_sweep_config(),
    )

    # An unscoped sweep (no project_id, not global_only) must never silently
    # default to platform truth across all projects — that was the contamination
    # bug. Such runs must fan out through run_all_due_projects instead.
    with pytest.raises(ValueError, match="run_all_due_projects"):
        await service._build_truth_digest_async(DreamRunOptions())


def test_completed_mutation_count_coerces_string_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        _completed_mutation_count(
            {"success": True, "run": {"id": "r", "summary": {"mutations": "2"}}}
        )
        == 2
    )
    caplog.set_level("WARNING", logger="gobby.memory.dream.aggregate")
    assert (
        _completed_mutation_count(
            {"success": True, "run": {"id": "r", "summary": {"mutations": "bad"}}}
        )
        == 0
    )
    assert "Invalid memory dream mutation count: value='bad' type=str" in caplog.text


def test_completed_mutation_count_raises_on_bad_result() -> None:
    with pytest.raises(RuntimeError, match="non-object"):
        _completed_mutation_count("nope")
    with pytest.raises(RuntimeError, match="boom"):
        _completed_mutation_count({"success": False, "error": "boom"})
    with pytest.raises(RuntimeError, match="without run_id"):
        _completed_mutation_count({"success": True, "run": {"summary": {"mutations": 1}}})


def test_result_run_id_prefers_top_level_then_nested() -> None:
    assert _result_run_id({"run_id": "top", "run": {"id": "nested"}}) == "top"
    assert _result_run_id({"run": {"id": "nested"}}) == "nested"
    assert _result_run_id({"success": True}) is None
    assert _result_run_id("not a dict") is None


class _FencedCursor:
    def __init__(
        self,
        row: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FencedConn:
    """Executes transactional dream action SQL against _FakeDreamDB state."""

    def __init__(self, db: _FakeDreamDB) -> None:
        self.db = db

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FencedCursor:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT * FROM memories") and "FOR UPDATE" in normalized:
            row = self.db.memories.get(str(params[0]))
            if row is None:
                return _FencedCursor()
            if "dream_due_version = %s" in normalized:
                if row.get("deleted_at") is not None:
                    return _FencedCursor()
                fence = (
                    row.get("dream_due_version", 0),
                    row["updated_at"],
                    row["project_id"],
                    row["is_global"],
                )
                if fence != (params[1], params[2], params[3], params[4]):
                    return _FencedCursor()
            return _FencedCursor(row=dict(row))
        if "FROM memory_crossrefs" in normalized:
            memory_id = str(params[0])
            rows = [dict(row) for key, row in self.db.crossrefs.items() if memory_id in key]
            return _FencedCursor(rows=rows)
        if normalized.startswith("INSERT INTO memory_dream_snapshots"):
            snapshot = {
                "id": len(self.db.snapshots) + 1,
                "run_id": params[0],
                "memory_id": params[1],
                "action": params[2],
                "before_data": params[3],
                "after_data": None,
                "applied": False,
            }
            self.db.snapshots.append(snapshot)
            return _FencedCursor(row={"id": snapshot["id"]})
        if normalized.startswith("SELECT id FROM memories WHERE content"):
            content, project_id, is_global, self_id = params
            for memory_id, row in self.db.memories.items():
                if (
                    memory_id != str(self_id)
                    and row["content"] == content
                    and row["project_id"] == project_id
                    and row["is_global"] == is_global
                    and row.get("deleted_at") is None
                ):
                    return _FencedCursor(row={"id": memory_id})
            return _FencedCursor()
        if (
            normalized.startswith("UPDATE memories SET")
            and "dream_due_version = dream_due_version + 1" in normalized
        ):
            row = self.db.memories[str(params[-1])]
            assignments = normalized.split(" SET ", maxsplit=1)[1].split(" WHERE ", maxsplit=1)[0]
            param_index = 0
            for assignment in assignments.split(", "):
                column, value_sql = assignment.split(" = ", maxsplit=1)
                if value_sql == "%s":
                    row[column] = params[param_index]
                    param_index += 1
                elif value_sql == "NULL":
                    row[column] = None
                elif value_sql == "dream_due_version + 1":
                    row[column] = int(row.get(column, 0)) + 1
                elif value_sql == "TRUE":
                    row[column] = True
                else:
                    raise AssertionError(f"unexpected revert assignment: {assignment}")
            return _FencedCursor()
        if normalized.startswith("UPDATE memories SET last_dreamed_at = %s WHERE"):
            self.db.memories[str(params[1])]["last_dreamed_at"] = params[0]
            return _FencedCursor()
        if normalized.startswith("UPDATE memories SET last_dreamed_at = %s, deleted_at = %s"):
            row = self.db.memories[str(params[3])]
            row["last_dreamed_at"] = params[0]
            row["deleted_at"] = params[1]
            row["dream_action"] = params[2]
            return _FencedCursor()
        if normalized.startswith("UPDATE memories SET content = %s, tags = %s"):
            row = self.db.memories[str(params[4])]
            row["content"] = params[0]
            row["tags"] = params[1]
            row["updated_at"] = params[2]
            row["last_dreamed_at"] = params[3]
            row["vector_needs_reindex"] = True
            return _FencedCursor()
        if normalized.startswith("UPDATE memories SET content = %s, updated_at = %s"):
            row = self.db.memories[str(params[3])]
            row["content"] = params[0]
            row["updated_at"] = params[1]
            row["last_dreamed_at"] = params[2]
            row["vector_needs_reindex"] = True
            return _FencedCursor()
        if normalized.startswith("UPDATE memories SET is_global = TRUE"):
            row = self.db.memories[str(params[1])]
            row["is_global"] = True
            row["vector_needs_reindex"] = True
            row["last_dreamed_at"] = params[0]
            return _FencedCursor()
        if normalized.startswith("DELETE FROM memory_crossrefs"):
            memory_id = str(params[0])
            self.db.crossrefs = {
                key: row for key, row in self.db.crossrefs.items() if memory_id not in key
            }
            return _FencedCursor()
        if normalized.startswith("INSERT INTO memory_crossrefs"):
            key = (str(params[0]), str(params[1]))
            self.db.crossrefs[key] = {
                "source_id": key[0],
                "target_id": key[1],
                "similarity": float(params[2]),
                "created_at": params[3],
            }
            return _FencedCursor()
        if normalized.startswith("SELECT * FROM memories WHERE id ="):
            row = self.db.memories.get(str(params[0]))
            return _FencedCursor(row=dict(row) if row else None)
        if normalized.startswith("UPDATE memory_dream_snapshots"):
            snapshot = next(s for s in self.db.snapshots if s["id"] == int(params[1]))
            snapshot["after_data"] = params[0]
            snapshot["applied"] = True
            return _FencedCursor()
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return _FencedCursor(row={"pg_advisory_xact_lock_shared": None})
        if normalized.startswith("INSERT INTO embedding_projection_changes"):
            events = self.db.projection_changes
            events.append(
                {
                    "sequence": len(events) + 1,
                    "source_kind": params[0],
                    "source_id": params[1],
                    "is_tombstone": params[2],
                }
            )
            return _FencedCursor(row={"sequence": len(events)})
        raise AssertionError(f"unexpected SQL in fenced fake: {normalized[:100]}")


@pytest.mark.asyncio
async def test_apply_refresh_duplicate_content_logs_info_and_advances_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refresh colliding with live same-scope content is benign: INFO, not WARNING."""
    db = _FakeDreamDB()
    db.memories = {
        "refresh-me": _row("refresh-me", "stale wording"),
        "existing": _row("existing", "canonical wording"),
    }
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    with caplog.at_level(logging.INFO, logger="gobby.memory.dream.apply"):
        summary = await apply_dream_plan(
            memory_manager=_as_dream_manager(manager),
            store=store,
            run_id=run_id,
            actions=[
                DreamAction(
                    action="refresh",
                    memory_id="refresh-me",
                    content="canonical wording",
                    confidence=1,
                )
            ],
            candidates=[_candidate("refresh-me")],
            dry_run=False,
            reconcile_after_apply=False,
        )

    assert summary["errors"] == 1
    assert summary["error_details"] == [
        {
            "action": DreamAction(
                action="refresh",
                memory_id="refresh-me",
                content="canonical wording",
                confidence=1,
            ).to_dict(),
            "error": "Memory content already exists in this project/global scope",
        }
    ]
    assert summary["mutations"] == 0
    # The failed refresh must not strand the candidate: cooldown cursor advanced
    # and the original content survived untouched.
    assert db.memories["refresh-me"]["last_dreamed_at"] is not None
    assert db.memories["refresh-me"]["content"] == "stale wording"
    duplicate_records = [r for r in caplog.records if "already exists" in r.getMessage()]
    assert duplicate_records, "expected the duplicate collision to be logged"
    assert all(r.levelno == logging.INFO for r in duplicate_records)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_apply_action_os_failure_stays_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real I/O failures keep their WARNING severity and error accounting."""
    db = _FakeDreamDB()
    db.memories = {"refresh-me": _row("refresh-me", "stale wording")}
    manager = _FakeMemoryManager(db)
    store = _dream_store(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    with (
        patch.object(
            store,
            "apply_candidate_action",
            side_effect=OSError("disk unavailable"),
        ),
        caplog.at_level(logging.INFO, logger="gobby.memory.dream.apply"),
    ):
        summary = await apply_dream_plan(
            memory_manager=_as_dream_manager(manager),
            store=store,
            run_id=run_id,
            actions=[
                DreamAction(
                    action="refresh",
                    memory_id="refresh-me",
                    content="new wording",
                    confidence=1,
                )
            ],
            candidates=[_candidate("refresh-me")],
            dry_run=False,
            reconcile_after_apply=False,
        )

    assert summary["errors"] == 1
    assert summary["error_details"][0]["error"] == "disk unavailable"
    failure_records = [r for r in caplog.records if "disk unavailable" in r.getMessage()]
    assert failure_records, "expected the I/O failure to be logged"
    assert all(r.levelno == logging.WARNING for r in failure_records)


def test_duplicate_memory_content_error_is_value_error() -> None:
    """The MCP create/update surface catches ValueError; the subclass must stay one."""
    assert issubclass(DuplicateMemoryContentError, ValueError)
