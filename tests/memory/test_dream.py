from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import discover_stale_candidates
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
from gobby.memory.dream.service import MemoryDreamService, _decode_raw_plan_metadata
from gobby.memory.dream.storage import MemoryDreamStore

pytestmark = pytest.mark.unit


def _memory(
    memory_id: str,
    *,
    days_old: int = 90,
    access_count: int = 0,
) -> SimpleNamespace:
    when = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    return SimpleNamespace(
        id=memory_id,
        content=f"Memory {memory_id}",
        memory_type="fact",
        project_id="proj-1",
        source_type="agent",
        source_session_id=None,
        tags=[],
        access_count=access_count,
        created_at=when,
        updated_at=when,
        last_accessed_at=datetime.now(UTC).isoformat(),
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


@pytest.mark.asyncio
async def test_stale_candidate_discovery_reviews_high_access_old_memory() -> None:
    manager = MagicMock()
    manager.alist_memories = AsyncMock(side_effect=[[_memory("old-hot", access_count=99)], []])
    config = SimpleNamespace(
        stale_age_days=30,
        scan_limit=10,
        max_scan_rows=100,
        include_global_memories=True,
        candidate_page_timeout_seconds=10.0,
    )

    result = await discover_stale_candidates(manager, config, project_id="proj-1")

    assert [candidate.id for candidate in result] == ["old-hot"]
    assert result[0].access_count == 99


@pytest.mark.asyncio
async def test_stale_candidate_discovery_times_out_page_fetch() -> None:
    async def slow_list(*, limit: int | None, offset: int) -> list[Any]:
        pending: asyncio.Future[list[Any]] = asyncio.Future()
        return await pending

    manager = MagicMock()
    manager.alist_memories = slow_list
    config = SimpleNamespace(
        stale_age_days=30,
        scan_limit=10,
        max_scan_rows=100,
        include_global_memories=True,
        candidate_page_timeout_seconds=0.001,
    )

    with pytest.raises(TimeoutError, match="listing memory dream candidates at offset 0"):
        await discover_stale_candidates(manager, config, project_id="proj-1")


@pytest.mark.asyncio
async def test_stale_candidate_discovery_ties_by_created_at() -> None:
    manager = MagicMock()
    older = _memory("older")
    newer = _memory("newer")
    for memory in (older, newer):
        memory.updated_at = "2025-01-01T00:00:00+00:00"
    older.created_at = "2024-01-01T00:00:00+00:00"
    newer.created_at = "2024-02-01T00:00:00+00:00"
    manager.alist_memories = AsyncMock(side_effect=[[newer, older], []])
    config = SimpleNamespace(
        stale_age_days=30,
        scan_limit=10,
        max_scan_rows=100,
        include_global_memories=True,
        candidate_page_timeout_seconds=10.0,
    )

    result = await discover_stale_candidates(
        manager,
        config,
        project_id="proj-1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert [candidate.id for candidate in result] == ["older", "newer"]


@pytest.mark.asyncio
async def test_stale_candidate_discovery_sorts_invalid_created_at_last() -> None:
    manager = MagicMock()
    valid = _memory("valid")
    invalid = _memory("invalid")
    for memory in (valid, invalid):
        memory.updated_at = "2025-01-01T00:00:00+00:00"
    valid.created_at = "2024-01-01T00:00:00+00:00"
    invalid.created_at = "not-a-date"
    manager.alist_memories = AsyncMock(side_effect=[[invalid, valid], []])
    config = SimpleNamespace(
        stale_age_days=30,
        scan_limit=10,
        max_scan_rows=100,
        include_global_memories=True,
        candidate_page_timeout_seconds=10.0,
    )

    result = await discover_stale_candidates(
        manager,
        config,
        project_id="proj-1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert [candidate.id for candidate in result] == ["valid", "invalid"]


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
        candidate.id
        for call in planner.call_args_list
        for candidate in call.kwargs["candidates"]
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
    assert planner.call_args.kwargs["duplicate_groups"] == []
    merge_actions = [action for action in plan["actions"] if action["action"] == "merge"]
    assert len(merge_actions) == 1
    assert merge_actions[0]["memory_ids"] == ["m1", "m2"]
    keep_ids = [
        action["memory_id"] for action in plan["actions"] if action["action"] == "keep"
    ]
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
                dream_config=SimpleNamespace(
                    planner_batch_size=1, planner_max_concurrency=cap
                ),
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


@pytest.mark.asyncio
async def test_stale_candidate_discovery_uses_defaults_for_bad_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    old = _memory("old")
    old.updated_at = "2025-01-01T00:00:00+00:00"
    manager.alist_memories = AsyncMock(side_effect=[[old], []])
    config = SimpleNamespace(
        stale_age_days="bad",
        scan_limit=False,
        max_scan_rows=0,
        include_global_memories="yes",
        candidate_page_timeout_seconds="bad",
    )

    result = await discover_stale_candidates(
        manager,
        config,
        project_id="proj-1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert [candidate.id for candidate in result] == ["old"]
    assert "using default" in caplog.text


def test_plan_validation_degrades_bad_or_omitted_actions_to_review() -> None:
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
    )

    by_id = {action.memory_id: action for action in actions if action.memory_id}
    assert by_id["a"].action == "review"
    assert by_id["b"].action == "review"
    assert by_id["c"].action == "review"
    assert by_id["d"].action == "review"


def test_plan_validation_splits_invalid_and_missing_id_review_reasons() -> None:
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
    )

    no_id_reasons = [action.reason for action in actions if action.memory_id is None]
    assert no_id_reasons == ["unknown candidate id", "missing candidate id"]
    assert any(
        action.memory_id == "a" and action.reason == "candidate omitted from dream plan"
        for action in actions
    )


def test_plan_validation_reviews_overlaps_and_restricts_action_to_new_ids() -> None:
    actions = validate_dream_plan(
        {
            "actions": [
                {"action": "delete", "memory_id": "a", "confidence": 1.0},
                {
                    "action": "merge",
                    "memory_ids": ["a", "b"],
                    "content": "merged",
                    "confidence": 1.0,
                },
            ]
        },
        [_candidate("a"), _candidate("b")],
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
    )

    assert any(action.memory_id == "a" and action.action == "delete" for action in actions)
    assert any(
        action.memory_id == "a"
        and action.action == "review"
        and action.reason == "candidate had overlapping dream actions"
        for action in actions
    )
    assert any(
        action.memory_id == "b"
        and action.action == "review"
        and action.reason == "merge requires at least two candidate ids"
        for action in actions
    )


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


def test_malformed_plan_reviews_all_candidates() -> None:
    actions = validate_dream_plan(
        "not-json",
        [_candidate("a"), _candidate("b")],
        min_action_confidence=0.7,
        min_delete_confidence=0.85,
    )

    assert {action.memory_id for action in actions} == {"a", "b"}
    assert {action.action for action in actions} == {"review"}


@pytest.mark.asyncio
async def test_apply_and_revert_delete_refresh_merge_and_supersede() -> None:
    db = _FakeDreamDB()
    db.memories = {
        "delete-me": _row("delete-me", "junk"),
        "refresh-me": _row("refresh-me", "old"),
        "merge-keep": _row("merge-keep", "dup"),
        "merge-drop": _row("merge-drop", "dup"),
        "supersede-me": _row("supersede-me", "old fact"),
    }
    manager = _FakeMemoryManager(db)
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id="proj-1", dry_run=False, options={})
    actions = [
        DreamAction(action="delete", memory_id="delete-me", confidence=1),
        DreamAction(action="refresh", memory_id="refresh-me", content="new", confidence=1),
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

    assert summary["mutations"] == 6
    assert "delete-me" not in db.memories
    assert db.memories["refresh-me"]["content"] == "new"
    assert "merge-drop" not in db.memories
    created_id = next(mid for mid in db.memories if mid.startswith("created-"))
    assert {row["action"] for row in db.snapshots} >= {
        "delete",
        "refresh",
        "merge",
        "supersede",
    }
    assert "supersede_create" not in {row["action"] for row in db.snapshots}

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is True
    assert db.memories["delete-me"]["content"] == "junk"
    assert db.memories["refresh-me"]["content"] == "old"
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
