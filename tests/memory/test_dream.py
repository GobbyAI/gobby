from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import list_sweep_candidates, memory_to_candidate
from gobby.memory.dream.duplicates import find_duplicate_groups
from gobby.memory.dream.models import (
    CONTENT_TRUNCATE_LIMIT,
    CONTENT_TRUNCATION_MARKER,
    DreamAction,
    DreamCandidate,
    DuplicateGroup,
)
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.planner import build_raw_plan
from gobby.memory.dream.service import (
    DreamRunOptions,
    MemoryDreamService,
    _decode_raw_plan_metadata,
)
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.memory.dream.truth_digest import build_current_truth_digest

pytestmark = pytest.mark.unit


def _memory(
    memory_id: str,
    *,
    days_old: int = 90,
    access_count: int = 0,
    project_id: str | None = "proj-1",
    last_dreamed_at: str | None = None,
) -> SimpleNamespace:
    when = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    return SimpleNamespace(
        id=memory_id,
        content=f"Memory {memory_id}",
        memory_type="fact",
        project_id=project_id,
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
    return DreamCandidate(
        id=memory_id,
        content=f"content {memory_id}",
        memory_type="fact",
        project_id="proj-1",
        source_type="agent",
        source_session_id=None,
        tags=[],
        age_days=90,
        access_count=100,
        created_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T00:00:00+00:00",
        last_accessed_at="2026-01-01T00:00:00+00:00",
    )


def test_candidate_prompt_content_marks_truncation() -> None:
    candidate = replace(_candidate("long"), content="x" * (CONTENT_TRUNCATE_LIMIT + 1))

    prompt = candidate.to_prompt_dict()

    assert prompt["content"].endswith(CONTENT_TRUNCATION_MARKER)
    assert len(prompt["content"]) == CONTENT_TRUNCATE_LIMIT


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
        project_id: str | None = None,
        memory_type: str | None = None,
        include_global: bool = True,
    ) -> list[Any]:
        self.calls.append(
            {
                "limit": limit,
                "redream_cutoff": redream_cutoff,
                "project_id": project_id,
                "memory_type": memory_type,
                "include_global": include_global,
            }
        )
        return list(self.rows)


@pytest.mark.asyncio
async def test_list_sweep_candidates_adapts_rows_and_forwards_scope() -> None:
    source = _RecordingSweepSource([_memory("m1", last_dreamed_at="2026-01-01T00:00:00+00:00")])

    result = await list_sweep_candidates(
        source,
        limit=50,
        redream_cutoff="2026-06-14T00:00:00+00:00",
        project_id="proj-1",
        memory_type="fact",
        include_global=False,
        now=datetime(2026, 6, 15, tzinfo=UTC),
    )

    assert [candidate.id for candidate in result] == ["m1"]
    assert source.calls == [
        {
            "limit": 50,
            "redream_cutoff": "2026-06-14T00:00:00+00:00",
            "project_id": "proj-1",
            "memory_type": "fact",
            "include_global": False,
        }
    ]
    assert "re-dream cooldown elapsed" in result[0].reasons


@pytest.mark.asyncio
async def test_list_sweep_candidates_flags_never_dreamed_and_global() -> None:
    source = _RecordingSweepSource([_memory("g1", project_id=None, last_dreamed_at=None)])

    result = await list_sweep_candidates(
        source,
        limit=10,
        redream_cutoff="2026-06-14T00:00:00+00:00",
    )

    assert result[0].project_id is None
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
            duplicate_groups=[],
            dream_config=SimpleNamespace(prompt_path="memory/dream"),
            llm_service=llm_service,
            db=None,
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert plan["actions"] == [{"action": "refresh"}]
    record = next(
        item
        for item in caplog.records
        if item.message == "Memory dream planner returned non-dict actions"
    )
    assert record.invalid_actions == ["invalid"]
    assert record.project_id == "proj-1"
    assert record.candidate_ids == ["memory-1"]


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
            duplicate_groups=[],
            dream_config=SimpleNamespace(planner_batch_size=2),
            llm_service=MagicMock(),
            db=None,
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
            duplicate_groups=[],
            dream_config=SimpleNamespace(planner_batch_size=1, planner_max_concurrency=1),
            llm_service=MagicMock(),
            db=None,
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert plan["planner_errors"] == ["page boom"]
    assert {action["memory_id"] for action in plan["actions"]} == {"m0", "m2"}


@pytest.mark.asyncio
async def test_build_raw_plan_excludes_duplicate_members_and_merges_once() -> None:
    candidates = [_candidate("m0"), _candidate("m1"), _candidate("m2")]
    groups = [
        DuplicateGroup(
            memory_ids=["m1", "m2"],
            canonical_content="canonical",
            reason="exact duplicate",
        )
    ]
    planner = AsyncMock(return_value={"actions": [{"action": "keep", "memory_id": "m0"}]})

    with patch("gobby.memory.dream.planner._call_llm_planner", planner):
        plan = await build_raw_plan(
            candidates=candidates,
            duplicate_groups=groups,
            dream_config=SimpleNamespace(planner_batch_size=25),
            llm_service=MagicMock(),
            db=None,
            project_id="proj-1",
            skip_consolidation=False,
        )

    assert planner.call_count == 1
    planned_ids = [candidate.id for candidate in planner.call_args.kwargs["candidates"]]
    assert planned_ids == ["m0"]  # duplicate members are not sent to the planner
    merge_actions = [action for action in plan["actions"] if action["action"] == "merge"]
    assert len(merge_actions) == 1
    assert merge_actions[0]["memory_ids"] == ["m1", "m2"]
    keep_ids = [action["memory_id"] for action in plan["actions"] if action["action"] == "keep"]
    assert keep_ids == ["m0"]


@pytest.mark.asyncio
async def test_build_raw_plan_limits_planner_concurrency() -> None:
    candidates = [_candidate(f"m{i}") for i in range(6)]
    cap = 2
    active = 0
    max_active = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def fake_planner(**_kwargs: Any) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active >= cap:
            saturated.set()
        await release.wait()
        active -= 1
        return {"actions": []}

    with patch("gobby.memory.dream.planner._call_llm_planner", fake_planner):
        task = asyncio.create_task(
            build_raw_plan(
                candidates=candidates,
                duplicate_groups=[],
                dream_config=SimpleNamespace(planner_batch_size=1, planner_max_concurrency=cap),
                llm_service=MagicMock(),
                db=None,
                project_id="proj-1",
                skip_consolidation=False,
            )
        )
        # The first wave fills the cap; the semaphore must block any extra call.
        await saturated.wait()
        assert active == cap
        release.set()
        plan = await task

    assert max_active == cap  # semaphore caps concurrent planner calls
    assert plan["planner_errors"] == []


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


def test_duplicate_groups_choose_canonical_without_quadratic_index_lookup() -> None:
    older = replace(
        _candidate("older"),
        content="same",
        created_at="2024-01-01T00:00:00+00:00",
    )
    newer = replace(
        _candidate("newer"),
        content="same",
        created_at="2024-02-01T00:00:00+00:00",
    )

    groups = find_duplicate_groups([newer, older])

    assert len(groups) == 1
    assert groups[0].memory_ids == ["older", "newer"]
    assert groups[0].canonical_content == "same"


def test_duplicate_groups_choose_longest_content_as_canonical() -> None:
    shorter = replace(
        _candidate("shorter"),
        content="same",
        created_at="2024-01-01T00:00:00+00:00",
    )
    longer = replace(
        _candidate("longer"),
        content="Same  ",
        created_at="2024-02-01T00:00:00+00:00",
    )

    groups = find_duplicate_groups([shorter, longer])

    assert len(groups) == 1
    assert groups[0].memory_ids == ["shorter", "longer"]
    assert groups[0].canonical_content == "Same  "


def test_duplicate_groups_ignore_non_string_content() -> None:
    candidate = replace(_candidate("bad"), content=None)

    assert find_duplicate_groups([candidate]) == []


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
    manager = _FakeMemoryManager(db)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    actions = [
        DreamAction(action="delete", memory_id="hide-me", confidence=1),
        DreamAction(action="review", memory_id="review-me", confidence=1),
        DreamAction(action="refresh", memory_id="refresh-me", content="new", confidence=1),
        DreamAction(action="keep", memory_id="keep-me", confidence=1),
    ]

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=actions,
        candidates=[_candidate("keep-me")],
        dry_run=False,
        reconcile_after_apply=False,
    )

    # keep is not a mutation; delete/review/refresh each count once.
    assert summary["mutations"] == 3
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
    # keep is stamp-only — no snapshot for it.
    assert {row["action"] for row in db.snapshots} == {"delete", "review", "refresh"}

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is True
    # Revert restores the mutating snapshots (delete/review/refresh) to active.
    assert db.memories["hide-me"]["deleted_at"] is None
    assert db.memories["hide-me"]["dream_action"] is None
    assert db.memories["review-me"]["deleted_at"] is None
    assert db.memories["refresh-me"]["content"] == "old"


@pytest.mark.asyncio
async def test_apply_and_revert_promote_rescopes_without_updated_at_bump() -> None:
    db = _FakeDreamDB()
    db.memories = {"promote-me": _row("promote-me", "universal")}
    before_updated_at = db.memories["promote-me"]["updated_at"]
    manager = _FakeMemoryManager(db)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="promote", memory_id="promote-me", confidence=0.9)],
        candidates=[_candidate("promote-me")],
        dry_run=False,
        reconcile_after_apply=False,
        when="2026-01-01T00:00:00+00:00",
    )

    assert summary["mutations"] == 1
    assert db.memories["promote-me"]["project_id"] is None
    assert db.memories["promote-me"]["updated_at"] == before_updated_at
    assert db.memories["promote-me"]["last_dreamed_at"] == "2026-01-01T00:00:00+00:00"
    assert {row["action"] for row in db.snapshots} == {"promote"}
    manager.sync_memory_scope_indices.assert_any_await("promote-me", None)

    result = await revert_dream_run(store=store, run_id=run_id, memory_manager=manager)

    assert result["success"] is True
    assert db.memories["promote-me"]["project_id"] == "proj-1"
    assert db.memories["promote-me"]["updated_at"] == before_updated_at
    manager.sync_memory_scope_indices.assert_any_await("promote-me", "proj-1")


@pytest.mark.asyncio
async def test_promote_already_global_stamps_cooldown_without_snapshot() -> None:
    db = _FakeDreamDB()
    db.memories = {"global": _row("global", "universal")}
    db.memories["global"]["project_id"] = None
    manager = _FakeMemoryManager(db)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="promote", memory_id="global", confidence=0.9)],
        candidates=[_candidate("global")],
        dry_run=False,
        reconcile_after_apply=False,
        when="2026-01-01T00:00:00+00:00",
    )

    assert summary["mutations"] == 0
    assert db.memories["global"]["last_dreamed_at"] == "2026-01-01T00:00:00+00:00"
    assert store.list_snapshots(run_id) == []
    manager.rescope_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_promote_rolls_back_scope_and_resyncs_secondary_scope() -> None:
    db = _FakeDreamDB()
    db.memories = {"promote-me": _row("promote-me", "universal")}
    manager = _FakeMemoryManager(db)
    manager.mark_dreamed = MagicMock(side_effect=OSError("stamp failed"))
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="promote", memory_id="promote-me", confidence=0.9)],
        candidates=[_candidate("promote-me")],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert summary["errors"] == 1
    assert summary["mutations"] == 0
    assert db.memories["promote-me"]["project_id"] == "proj-1"
    manager.sync_memory_scope_indices.assert_any_await("promote-me", None)
    manager.sync_memory_scope_indices.assert_any_await("promote-me", "proj-1")


@pytest.mark.asyncio
async def test_apply_and_revert_legacy_merge_and_supersede() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "merge-keep": _row("merge-keep", "dup"),
        "merge-drop": _row("merge-drop", "dup"),
        "supersede-me": _row("supersede-me", "old fact"),
    }
    manager = _FakeMemoryManager(db)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    actions = [
        DreamAction(
            action="merge",
            memory_ids=["merge-keep", "merge-drop"],
            content="merged",
            confidence=1,
        ),
        DreamAction(action="supersede", memory_id="supersede-me", content="new fact", confidence=1),
    ]

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=actions,
        candidates=[_candidate("supersede-me")],
        dry_run=False,
        reconcile_after_apply=False,
    )

    # merge: keeper update + duplicate delete; supersede: create + original delete.
    assert summary["mutations"] == 4
    assert db.memories["merge-keep"]["content"] == "merged"
    assert "merge-drop" not in db.memories
    created_id = next(mid for mid in db.memories if mid.startswith("created-"))
    assert "supersede-me" not in db.memories
    assert {row["action"] for row in db.snapshots} >= {"merge", "supersede"}

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is True
    assert db.memories["merge-keep"]["content"] == "dup"
    assert db.memories["merge-drop"]["content"] == "dup"
    assert db.memories["supersede-me"]["content"] == "old fact"
    assert created_id not in db.memories


@pytest.mark.asyncio
async def test_apply_dream_plan_dry_run_includes_planned_action_preview() -> None:
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=True, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="refresh", memory_id="memory-1", content="new")],
        candidates=[_candidate("memory-1")],
        dry_run=True,
        reconcile_after_apply=False,
    )

    assert summary["mutations"] == 0
    assert summary["snapshots"] == 0
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
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
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


@pytest.mark.asyncio
async def test_merge_rolls_back_keeper_update_when_duplicate_delete_fails() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "merge-keep": _row("merge-keep", "old"),
        "merge-drop": _row("merge-drop", "old"),
    }
    manager = _FakeMemoryManager(db)

    async def delete_memory(memory_id: str) -> bool:
        if memory_id == "merge-drop":
            await manager._delete(memory_id)
            raise OSError("delete failed")
        return await manager._delete(memory_id)

    manager.delete_memory = AsyncMock(side_effect=delete_memory)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[
            DreamAction(
                action="merge",
                memory_ids=["merge-keep", "merge-drop"],
                content="merged",
            )
        ],
        candidates=[],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert summary["errors"] == 1
    assert summary["mutations"] == 0
    assert db.memories["merge-keep"]["content"] == "old"
    assert db.memories["merge-drop"]["content"] == "old"
    assert store.list_snapshots(run_id) == []
    assert [snapshot["applied"] for snapshot in db.snapshots] == [False, False]


@pytest.mark.asyncio
async def test_supersede_deletes_created_replacement_when_original_delete_fails() -> None:
    db = _FakeDreamDB()
    db.memories = {"supersede-me": _row("supersede-me", "old")}
    manager = _FakeMemoryManager(db)

    async def delete_memory(memory_id: str) -> bool:
        if memory_id == "supersede-me":
            await manager._delete(memory_id)
            raise OSError("delete failed")
        return await manager._delete(memory_id)

    manager.delete_memory = AsyncMock(side_effect=delete_memory)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="supersede", memory_id="supersede-me", content="new")],
        candidates=[_candidate("supersede-me")],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert summary["errors"] == 1
    assert summary["mutations"] == 0
    assert set(db.memories) == {"supersede-me"}
    assert db.memories["supersede-me"]["content"] == "old"


async def test_revert_dream_run_uses_newest_first_snapshots_without_reversal() -> None:
    db = _FakeDreamDB()
    db.memories = {"memory-1": _row("memory-1", "v3")}
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    store.record_applied_snapshot(
        run_id=run_id,
        memory_id="memory-1",
        action="refresh",
        before_data=_row("memory-1", "v1"),
        after_data=_row("memory-1", "v2"),
    )
    store.record_applied_snapshot(
        run_id=run_id,
        memory_id="memory-1",
        action="refresh",
        before_data=_row("memory-1", "v2"),
        after_data=_row("memory-1", "v3"),
    )

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is True
    assert db.memories["memory-1"]["content"] == "v1"


@pytest.mark.asyncio
async def test_revert_dream_run_marks_revert_failed_and_continues_on_snapshot_error() -> None:
    db = _FakeDreamDB()
    db.memories = {"good": _row("good", "new")}
    store = MemoryDreamStore(db)
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
    store = MemoryDreamStore(db)
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
        memory_manager=manager,
        dream_config=SimpleNamespace(reconcile_after_revert=False),
    )
    revert_mock = AsyncMock(return_value={"success": True, "run_id": "dream-1"})

    with patch("gobby.memory.dream.service.revert_dream_run", revert_mock):
        result = await service.revert("dream-1")

    assert result["success"] is True
    assert revert_mock.await_args.kwargs["reconcile_after_revert"] is False


def test_memory_dream_service_record_run_failure_is_idempotent() -> None:
    db = _FakeDreamDB()
    manager = _FakeMemoryManager(db)
    service = MemoryDreamService(memory_manager=manager, dream_config=SimpleNamespace())
    run_id = service.store.create_run(project_id="proj-1", dry_run=False, options={})

    failed = service.record_run_failure(run_id, "boom")
    repeated = service.record_run_failure(run_id, "later")

    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"
    assert repeated is not None
    assert repeated["error"] == "boom"


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
        "memory_type": "fact",
        "content": content,
        "source_type": "agent",
        "source_session_id": None,
        "access_count": 0,
        "last_accessed_at": None,
        "tags": [],
        "graph_processed": True,
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
        "deleted_at": None,
        "dream_action": None,
        "last_dreamed_at": None,
    }


class _Cursor:
    rowcount = 1


class _FakeDreamDB:
    dialect = "postgres"

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.snapshots: list[dict[str, Any]] = []

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
                "created_at": params[6],
                "updated_at": params[7],
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
        elif normalized.startswith("DELETE FROM memories"):
            self.memories.pop(str(params[0]), None)
        elif normalized.startswith("INSERT INTO memories"):
            columns = (
                "id",
                "project_id",
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
            self.memories[str(params[0])] = dict(zip(columns, params, strict=True))
        return _Cursor()

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        normalized = " ".join(sql.split())
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
        if normalized.startswith("SELECT * FROM memory_dream_runs"):
            return self.runs.get(str(params[0]))
        if "FROM memories" in normalized:
            row = self.memories.get(str(params[0]))
            return dict(row) if row else None
        return None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if "FROM memory_dream_snapshots" not in normalized:
            return []
        run_id = str(params[0])
        rows = [row for row in self.snapshots if row["run_id"] == run_id and row["applied"]]
        return sorted(rows, key=lambda row: row["id"], reverse=True)

    def _snapshot(self, snapshot_id: int) -> dict[str, Any]:
        return next(row for row in self.snapshots if row["id"] == snapshot_id)


def test_update_run_rejects_unknown_fields() -> None:
    db = _FakeDreamDB()
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})

    with pytest.raises(ValueError, match="unknown_column"):
        store.update_run(run_id, unknown_column="bad")


def test_restore_memory_row_rejects_incomplete_snapshot() -> None:
    store = MemoryDreamStore(_FakeDreamDB())
    row = _row("memory-1", "content")
    row.pop("updated_at")

    with pytest.raises(ValueError, match="missing columns: updated_at"):
        store.restore_memory_row(row)


class _FakeMemoryManager:
    def __init__(self, db: _FakeDreamDB) -> None:
        self.db = db
        self.delete_memory = AsyncMock(side_effect=self._delete)
        self.update_memory = AsyncMock(side_effect=self._update)
        self.create_memory = AsyncMock(side_effect=self._create)
        self.rescope_memory = AsyncMock(side_effect=self._rescope)
        self.sync_memory_scope_indices = AsyncMock(return_value=[])

    async def _delete(self, memory_id: str) -> bool:
        return self.db.memories.pop(memory_id, None) is not None

    async def _update(
        self,
        *,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Any:
        if content is not None:
            self.db.memories[memory_id]["content"] = content
        if tags is not None:
            self.db.memories[memory_id]["tags"] = tags
        return SimpleNamespace(id=memory_id)

    async def _create(self, **kwargs: Any) -> Any:
        memory_id = f"created-{len(self.db.memories)}"
        self.db.memories[memory_id] = _row(memory_id, kwargs["content"])
        return SimpleNamespace(id=memory_id)

    async def _rescope(self, memory_id: str, new_project_id: str | None) -> Any:
        row = self.db.memories.get(memory_id)
        if row is None:
            raise ValueError(f"Memory {memory_id} not found")
        row["project_id"] = new_project_id
        await self.sync_memory_scope_indices(memory_id, new_project_id)
        return SimpleNamespace(
            id=memory_id,
            project_id=new_project_id,
            updated_at=row["updated_at"],
            content=row["content"],
        )

    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: str | None = None,
        when: str | None = None,
    ) -> bool:
        row = self.db.memories.get(memory_id)
        if row is None:
            raise ValueError(f"Memory {memory_id} not found")
        stamp = when or datetime.now(UTC).isoformat()
        row["last_dreamed_at"] = stamp
        if hidden_as is not None:
            row["deleted_at"] = stamp
            row["dream_action"] = hidden_as
        return True


class _FakeSweepManager:
    """Stateful manager exercising the streaming sweep cooldown query in memory."""

    def __init__(self, db: _FakeDreamDB) -> None:
        self.db = db

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        project_id: str | None = None,
        memory_type: str | None = None,
        include_global: bool = True,
    ) -> list[Any]:
        matches: list[dict[str, Any]] = []
        for row in self.db.memories.values():
            if row.get("deleted_at") is not None:
                continue
            last_dreamed = row.get("last_dreamed_at")
            if last_dreamed is not None and last_dreamed >= redream_cutoff:
                continue
            if project_id is not None:
                row_project = row.get("project_id")
                in_scope = row_project == project_id or (include_global and row_project is None)
                if not in_scope:
                    continue
            if memory_type is not None and row.get("memory_type") != memory_type:
                continue
            matches.append(row)
        matches.sort(key=lambda r: (r.get("last_dreamed_at") or "", r.get("updated_at") or ""))
        return [SimpleNamespace(**row) for row in matches[:limit]]

    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: str | None = None,
        when: str | None = None,
    ) -> bool:
        row = self.db.memories.get(memory_id)
        if row is None:
            raise ValueError(f"Memory {memory_id} not found")
        stamp = when or datetime.now(UTC).isoformat()
        row["last_dreamed_at"] = stamp
        if hidden_as is not None:
            row["deleted_at"] = stamp
            row["dream_action"] = hidden_as
        return True

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        return {}

    async def update_memory(
        self,
        *,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Any:
        if content is not None:
            self.db.memories[memory_id]["content"] = content
        return SimpleNamespace(id=memory_id)

    async def delete_memory(self, memory_id: str) -> bool:
        return self.db.memories.pop(memory_id, None) is not None


def _sweep_config(*, page_size: int = 2, redream_after_hours: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
        min_rescope_confidence=0.85,
        reconcile_after_apply=False,
        reconcile_after_revert=False,
        page_size=page_size,
        redream_after_hours=redream_after_hours,
        include_global_memories=True,
    )


@pytest.mark.asyncio
async def test_streaming_sweep_drains_and_immediate_rerun_is_noop() -> None:
    db = _FakeDreamDB()
    db.memories = {f"m{i}": _row(f"m{i}", f"content {i}") for i in range(5)}
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=manager, dream_config=_sweep_config(page_size=2), llm_service=None
    )

    result = await service.run(DreamRunOptions())

    assert result["success"] is True
    summary = result["run"]["summary"]
    assert summary["candidates_reviewed"] == 5
    assert summary["pages"] == 3  # ceil(5 / 2)
    assert summary["mutations"] == 0
    assert summary["actions"].get("keep") == 5
    assert all(row["last_dreamed_at"] is not None for row in db.memories.values())

    rerun = await service.run(DreamRunOptions())

    # Everything was just stamped inside the cooldown window, so the re-run is a no-op.
    assert rerun["success"] is True
    assert rerun["run"]["summary"]["candidates_reviewed"] == 0
    assert rerun["run"]["summary"]["pages"] == 0


@pytest.mark.asyncio
async def test_streaming_sweep_soft_hides_obsolete_and_keeps_current() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "obsolete": _row("obsolete", "Graph backend is Neo4j"),
        "current": _row("current", "Graph backend is FalkorDB"),
    }
    manager = _FakeSweepManager(db)
    service = MemoryDreamService(
        memory_manager=manager, dream_config=_sweep_config(page_size=10), llm_service=MagicMock()
    )
    canned = {
        "actions": [
            {"action": "delete", "memory_id": "obsolete", "confidence": 1.0, "reason": "retired"}
        ],
        "planner_errors": [],
    }

    with patch("gobby.memory.dream.service.build_raw_plan", AsyncMock(return_value=canned)):
        result = await service.run(DreamRunOptions())

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
        memory_manager=manager, dream_config=_sweep_config(page_size=10), llm_service=MagicMock()
    )
    canned = {
        "actions": [{"action": "delete", "memory_id": "obsolete", "confidence": 1.0}],
        "planner_errors": [],
    }

    with patch("gobby.memory.dream.service.build_raw_plan", AsyncMock(return_value=canned)):
        result = await service.run(DreamRunOptions(dry_run=True))

    assert result["success"] is True
    # Dry-run is a single bounded preview pass: no memory, snapshot, or stamp writes.
    assert all(row["deleted_at"] is None for row in db.memories.values())
    assert all(row["last_dreamed_at"] is None for row in db.memories.values())
    summary = result["run"]["summary"]
    assert summary["mutations"] == 0
    assert summary["candidates_reviewed"] == 2
    assert summary["pages"] == 1
    assert summary.get("planned_actions")


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
    assert "SQLite" in prompt
