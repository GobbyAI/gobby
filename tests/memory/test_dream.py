from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import discover_stale_candidates
from gobby.memory.dream.models import DreamAction, DreamCandidate
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.storage import MemoryDreamStore

pytestmark = pytest.mark.unit


def _memory(memory_id: str, *, days_old: int = 90, access_count: int = 0) -> Any:
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


def test_stale_candidate_discovery_reviews_high_access_old_memory() -> None:
    manager = MagicMock()
    manager.list_memories.side_effect = [[_memory("old-hot", access_count=99)], []]
    config = SimpleNamespace(
        stale_age_days=30,
        scan_limit=10,
        max_scan_rows=100,
        include_global_memories=True,
    )

    result = discover_stale_candidates(manager, config, project_id="proj-1")

    assert [candidate.id for candidate in result] == ["old-hot"]
    assert result[0].access_count == 99


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

    result = await revert_dream_run(store=store, run_id=run_id)

    assert result["success"] is True
    assert db.memories["delete-me"]["content"] == "junk"
    assert db.memories["refresh-me"]["content"] == "old"
    assert db.memories["merge-drop"]["content"] == "dup"
    assert db.memories["supersede-me"]["content"] == "old fact"
    assert created_id not in db.memories


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
        "media": None,
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
            snapshot = self._snapshot(int(params[1]))
            snapshot["after_data"] = params[0]
            snapshot["applied"] = True
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
                "media",
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
        if "FROM memory_dream_snapshots" not in sql:
            return []
        run_id = str(params[0])
        rows = [row for row in self.snapshots if row["run_id"] == run_id and row["applied"]]
        return sorted(rows, key=lambda row: row["id"], reverse=True)

    def _snapshot(self, snapshot_id: int) -> dict[str, Any]:
        return next(row for row in self.snapshots if row["id"] == snapshot_id)


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
