"""Tests for the async embedding switch phase runner."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gobby.ai.embedding_switch import (
    PHASE_ABORTED,
    PHASE_ACTIVE,
    PHASE_BUILDING,
    PHASE_FLIPPING,
    PHASE_GC,
    PHASE_STAGING,
    SwitchJournal,
    get_switch_status,
)
from gobby.ai.embedding_switch_runner import EmbeddingSwitchRunner
from gobby.ai.embedding_switch_service import EmbeddingSwitchControl
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    EMBEDDING_SWITCH_COMPLETED_KEY,
    EMBEDDING_SWITCH_JOURNAL_KEY,
)
from gobby.storage.embedding_generation_state import ProjectionChange
from gobby.storage.github_triage import GitHubIssueTriageRecord, GitHubTriageStore

pytestmark = pytest.mark.unit


class FakeConfigStore:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.operations: list[tuple[Any, ...]] = []

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def get_all(self) -> dict[str, Any]:
        return {}

    def get_internal_lifecycle(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any, source: str = "user") -> None:
        self.values[key] = value
        self.operations.append(("set", key, source))

    def set_internal_lifecycle(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.operations.append(("lifecycle_set", key))

    def set_many(self, entries: dict[str, Any], source: str = "user") -> int:
        self.values.update(entries)
        self.operations.append(("set_many", dict(entries), source))
        return len(entries)

    def set_embedding_switch_values(self, run_id: str, entries: dict[str, Any]) -> int:
        self.values.update(entries)
        self.operations.append(("owner_set", run_id, dict(entries)))
        return len(entries)

    def complete_embedding_switch(
        self,
        run_id: str,
        entries: dict[str, Any],
        completed_key: str,
        completed_record: dict[str, Any],
    ) -> int:
        revision = 42
        self.values.update(entries)
        self.values[completed_key] = {**completed_record, "committed_revision": revision}
        self.values.pop(EMBEDDING_SWITCH_JOURNAL_KEY, None)
        self.operations.append(("complete", run_id, dict(entries), completed_key))
        return revision

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.operations.append(("delete", key))

    def delete_internal_lifecycle(self, key: str, run_id: str) -> bool:
        existed = self.values.pop(key, None) is not None
        self.operations.append(("lifecycle_delete", key, run_id))
        return existed


class _FakeCursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, object] | None:
        return self._row

    def fetchall(self) -> list[dict[str, object]]:
        return [] if self._row is None else [self._row]


class _FakeTransaction:
    def execute(self, query: str, params: tuple[object, ...] = ()) -> _FakeCursor:
        if "MAX(sequence)" in query:
            return _FakeCursor({"watermark": 0})
        return _FakeCursor(None)


class FakeDatabase:
    @contextmanager
    def transaction(self) -> Iterator[_FakeTransaction]:
        yield _FakeTransaction()

    def fetchone(self, query: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
        if "MAX(sequence)" in query:
            return {"watermark": 0}
        if "FROM memories" in query:
            return None
        return {"incompatible": 0}

    def fetchall(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        return []


class FakeVectorStore:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.aliases = aliases or {}
        self.operations: list[tuple[str, str, str | None]] = []
        self.ensured: list[str] = []

    async def ensure_collection(
        self,
        collection_name: str,
        embedding_dim: int | None = None,
        *,
        recreate_on_mismatch: bool = False,
    ) -> None:
        assert embedding_dim is not None
        assert recreate_on_mismatch is True
        self.ensured.append(collection_name)

    async def get_aliases(self) -> dict[str, str]:
        return dict(self.aliases)

    async def create_alias(self, collection_name: str, alias_name: str) -> None:
        self.operations.append(("alias", collection_name, alias_name))
        self.aliases[alias_name] = collection_name

    async def delete_collection(self, collection_name: str) -> None:
        self.operations.append(("delete", collection_name, None))

    async def delete(self, point_id: str, *, collection_name: str) -> None:
        self.operations.append(("delete", point_id, collection_name))

    async def upsert(
        self,
        point_id: str,
        embedding: list[float],
        payload: dict[str, Any],
        *,
        collection_name: str,
    ) -> None:
        self.operations.append(("upsert", point_id, collection_name))


class FakeEmbeddingService:
    async def generate_embedding(self, text: str) -> list[float]:
        return [float(len(text))]


def _journal(phase: str = PHASE_FLIPPING) -> SwitchJournal:
    return SwitchJournal(
        "4096-run",
        "qwen3-8b-q8",
        target_dim=4096,
        target_model="qwen3-embedding:8b-q8_0",
        target_query_prefix="query:",
        target_api_base=None,
        provider="ollama",
        phase=phase,
        started_at="2026-06-29T00:00:00Z",
        updated_at="2026-06-29T00:00:00Z",
    )


def _issue_record(*, source_text: str | None) -> GitHubIssueTriageRecord:
    return GitHubIssueTriageRecord(
        id="row-1",
        project_id="project-1",
        repo="owner/repo",
        issue_number=42,
        issue_url="https://github.com/owner/repo/issues/42",
        issue_state="open",
        labels=("bug",),
        issue_updated_at="2026-05-03T00:00:00Z",
        content_hash="hash-1",
        verdict="implement",
        decision_json="{}",
        task_id="task-1",
        vector_point_id="point-1",
        dedup_issue_key=None,
        source="webhook",
        source_text=source_text,
        last_triaged_at="2026-05-03T00:00:00Z",
        created_at="2026-05-03T00:00:00Z",
        updated_at="2026-05-03T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_run_staging_failure_keeps_staging_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = EmbeddingSwitchRunner(FakeConfigStore(), db=FakeDatabase())

    async def fail_stage(_journal: SwitchJournal) -> object:
        raise RuntimeError("stage failed")

    monkeypatch.setattr(runner, "stage", fail_stage)

    report = await runner.run(_journal(PHASE_STAGING))

    assert report.failed is True
    assert report.journal is not None
    assert report.journal.phase == PHASE_STAGING
    assert "stage failed" in (report.journal.error or "")


@pytest.mark.asyncio
async def test_flip_records_old_targets_before_config_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore(
        {
            "memories": "memories@old",
            "tool_embeddings": "tool_embeddings@old",
            "gobby_github_issues": "gobby_github_issues@old",
        }
    )
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)

    _result, journal = await runner.flip(_journal(PHASE_FLIPPING))

    assert journal.phase == PHASE_ACTIVE
    assert store.operations[0][0] == "lifecycle_set"
    assert store.operations[1][0] == "complete"
    run_id = store.operations[1][1]
    assert run_id == journal.run_id
    assert store.operations[1][2][AI_EMBEDDING_API_BASE_KEY] is None
    completed = store.values[EMBEDDING_SWITCH_COMPLETED_KEY]
    assert completed["committed_revision"] == 42
    assert completed["old_physical_names"]["memories"] == "memories@old"
    assert journal.old_physical_names["memories"] == "memories@old"
    assert ("alias", "memories@4096-run", "memories") in vector_store.operations
    assert not any(operation[0] == "delete" for operation in vector_store.operations)


@pytest.mark.asyncio
async def test_legacy_bare_collection_flip_deletes_old_bare_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)

    _result, journal = await runner.flip(_journal(PHASE_FLIPPING))

    assert journal.old_physical_names["memories"] == "memories"
    assert vector_store.operations.index(("alias", "memories@4096-run", "memories")) < (
        vector_store.operations.index(("delete", "memories", None))
    )


@pytest.mark.asyncio
async def test_gc_failure_keeps_gc_phase_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore({"memories": "memories@old"})
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    journal = _journal(PHASE_ACTIVE)
    journal.old_physical_names = {"memories": "memories@old"}

    report = await runner.run(journal)

    assert report.failed is True
    assert report.journal is not None
    assert report.journal.phase == PHASE_GC
    assert "still targeted by an alias" in (report.journal.error or "")


@pytest.mark.asyncio
async def test_build_uses_target_physical_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    monkeypatch.setattr(runner, "_embedding_service", lambda journal: FakeEmbeddingService())

    async def no_items(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(runner, "_build_memory_collection", no_items)
    monkeypatch.setattr(runner, "_build_tool_collection", no_items)
    monkeypatch.setattr(runner, "_build_github_issue_collection", no_items)

    _result, journal = await runner.build(_journal(PHASE_BUILDING))

    assert journal.phase == PHASE_FLIPPING
    assert vector_store.ensured == [
        "memories@4096-run",
        "tool_embeddings@4096-run",
        "gobby_github_issues@4096-run",
    ]


@pytest.mark.asyncio
async def test_build_replays_changes_after_enumeration_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    monkeypatch.setattr(runner, "_embedding_service", lambda journal: FakeEmbeddingService())
    builds: list[str] = []

    async def build_memory(*args: Any, **kwargs: Any) -> int:
        builds.append("memory")
        return 0

    async def build_tool(*args: Any, **kwargs: Any) -> int:
        builds.append("tool")
        return 0

    async def build_issue(*args: Any, **kwargs: Any) -> int:
        builds.append("github_issue")
        return 0

    class GenerationState:
        def __init__(self) -> None:
            self.watermark_calls = 0
            self.db_threads: list[int] = []

        def watermark(self) -> int:
            self.db_threads.append(threading.get_ident())
            self.watermark_calls += 1
            return 7 if self.watermark_calls == 1 else 9

        def changes_after(
            self, sequence: int, *, up_to: int | None = None
        ) -> list[ProjectionChange]:
            self.db_threads.append(threading.get_ident())
            changes = [
                ProjectionChange(8, "tool", "tool-1", False),
                ProjectionChange(9, "memory", "memory-1", True),
            ]
            return [
                change
                for change in changes
                if change.sequence > sequence and (up_to is None or change.sequence <= up_to)
            ]

    cast(Any, runner).generation_state = GenerationState()
    monkeypatch.setattr(runner, "_build_memory_collection", build_memory)
    monkeypatch.setattr(runner, "_build_tool_collection", build_tool)
    monkeypatch.setattr(runner, "_build_github_issue_collection", build_issue)

    projected: list[tuple[str, str]] = []

    async def project_change(
        journal: Any, service: Any, vector_store_arg: Any, change: ProjectionChange
    ) -> int:
        projected.append((change.source_kind, change.source_id))
        return 1

    monkeypatch.setattr(runner, "_project_change", project_change)

    _result, journal = await runner.build(_journal(PHASE_BUILDING))

    assert journal.physical_names["memories"] == "memories@4096-run"
    assert journal.caught_up_watermark == 9
    # Full corpus builders run only for the initial fill; replay is per-change.
    assert builds == ["memory", "tool", "github_issue"]
    assert projected == [("tool", "tool-1")]
    assert ("delete", "memory-1", "memories@4096-run") in vector_store.operations
    assert all(
        thread_id != threading.get_ident()
        for thread_id in cast(Any, runner.generation_state).db_threads
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [None, {"deleted_at": datetime.now(UTC)}],
    ids=["absent", "deleted"],
)
async def test_memory_change_deletes_absent_or_deleted_source(
    row: dict[str, object] | None,
) -> None:
    class MemoryDatabase(FakeDatabase):
        def fetchone(self, query: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
            if "FROM memories" in query:
                return row
            return super().fetchone(query, params)

    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(FakeConfigStore(), db=cast(Any, MemoryDatabase()))

    projected = await runner._project_memory_change(
        _journal(),
        cast(Any, FakeEmbeddingService()),
        cast(Any, vector_store),
        "memory-1",
    )

    assert projected == 0
    assert vector_store.operations == [("delete", "memory-1", "memories@4096-run")]


@pytest.mark.asyncio
async def test_memory_change_upserts_into_promoted_collection() -> None:
    now = datetime.now(UTC)
    row: dict[str, object] = {
        "id": "memory-1",
        "memory_type": "fact",
        "content": "remember this",
        "created_at": now,
        "updated_at": now,
        "project_id": "project-1",
        "source_type": "agent",
        "source_session_id": None,
        "access_count": 0,
        "last_accessed_at": None,
        "tags": ["important"],
        "deleted_at": None,
    }

    class MemoryDatabase(FakeDatabase):
        def fetchone(self, query: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
            if "FROM memories" in query:
                return row
            return super().fetchone(query, params)

    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(FakeConfigStore(), db=cast(Any, MemoryDatabase()))

    projected = await runner._project_memory_change(
        _journal(),
        cast(Any, FakeEmbeddingService()),
        cast(Any, vector_store),
        "memory-1",
    )

    assert projected == 1
    assert vector_store.operations == [("upsert", "memory-1", "memories@4096-run")]


@pytest.mark.asyncio
async def test_legacy_github_issue_without_source_text_leaves_building_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    monkeypatch.setattr(runner, "_embedding_service", lambda journal: FakeEmbeddingService())

    async def no_items(*args: Any, **kwargs: Any) -> int:
        return 0

    def legacy_records(
        self: GitHubTriageStore,
        *,
        project_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[GitHubIssueTriageRecord]:
        return [_issue_record(source_text=None)] if offset == 0 else []

    monkeypatch.setattr(runner, "_build_memory_collection", no_items)
    monkeypatch.setattr(runner, "_build_tool_collection", no_items)
    monkeypatch.setattr(GitHubTriageStore, "list_issue_records", legacy_records)

    report = await runner.run(_journal(PHASE_BUILDING))

    assert report.failed is True
    assert report.journal is not None
    assert report.journal.phase == PHASE_BUILDING
    assert "run GitHub triage reconcile/reprocessing" in (report.journal.error or "")


@pytest.mark.asyncio
async def test_abort_cleanup_failure_keeps_durable_aborted_journal_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    control = EmbeddingSwitchControl()
    control.abort_requested.set()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase(), control=control)
    monkeypatch.setattr(runner, "_vector_store", lambda _journal: vector_store)
    cleanup_attempts = 0

    async def fail_first_cleanup(collection_name: str) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise RuntimeError("qdrant cleanup failed")
        vector_store.operations.append(("delete", collection_name, None))

    monkeypatch.setattr(vector_store, "delete_collection", fail_first_cleanup)
    failed = await runner.run(_journal(PHASE_BUILDING))

    assert failed.failed is True
    assert failed.journal is not None
    assert failed.journal.phase == PHASE_ABORTED
    assert get_switch_status(store).phase == PHASE_ABORTED  # type: ignore[union-attr]

    resumed = await runner.run(failed.journal)

    assert resumed.failed is False
    assert resumed.journal is None
    assert get_switch_status(store) is None
    deleted_names = {
        name for operation, name, _alias in vector_store.operations if operation == "delete"
    }
    assert deleted_names == {
        "memories@4096-run",
        "tool_embeddings@4096-run",
        "gobby_github_issues@4096-run",
    }


async def test_abort_cleanup_refuses_to_delete_alias_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore({"memories": "memories@4096-run"})
    control = EmbeddingSwitchControl()
    control.abort_requested.set()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase(), control=control)
    monkeypatch.setattr(runner, "_vector_store", lambda _journal: vector_store)

    result = await runner.run(_journal(PHASE_BUILDING))

    assert result.failed is True
    assert result.journal is not None
    assert result.journal.phase == PHASE_ABORTED
    assert result.journal.error is not None
    assert "still targeted by an alias" in result.journal.error
    assert ("delete", "memories@4096-run", None) not in vector_store.operations
    persisted = get_switch_status(store)
    assert persisted is not None
    assert persisted.phase == PHASE_ABORTED


@pytest.mark.asyncio
async def test_build_persists_physical_names_before_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    monkeypatch.setattr(runner, "_embedding_service", lambda journal: FakeEmbeddingService())

    async def no_items(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(runner, "_build_memory_collection", no_items)
    monkeypatch.setattr(runner, "_build_tool_collection", no_items)
    monkeypatch.setattr(runner, "_build_github_issue_collection", no_items)

    journal_writes: list[dict[str, Any]] = []
    original_set = store.set_internal_lifecycle

    def record_lifecycle(key: str, value: Any) -> None:
        if key == EMBEDDING_SWITCH_JOURNAL_KEY:
            journal_writes.append(dict(value))
        original_set(key, value)

    monkeypatch.setattr(store, "set_internal_lifecycle", record_lifecycle)

    watermark_seen: list[bool] = []

    class GenerationState:
        def watermark(self) -> int:
            # Journal-first: physical_names must be persisted before this read.
            watermark_seen.append(any(write.get("physical_names") for write in journal_writes))
            return 3

        def changes_after(
            self, sequence: int, *, up_to: int | None = None
        ) -> list[ProjectionChange]:
            return []

    cast(Any, runner).generation_state = GenerationState()

    _result, journal = await runner.build(_journal(PHASE_BUILDING))

    assert watermark_seen and watermark_seen[0] is True
    assert journal.caught_up_watermark == 3
    assert any(write.get("caught_up_watermark") == 3 for write in journal_writes)


@pytest.mark.asyncio
async def test_replay_is_bounded_and_raises_after_max_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ai.embedding_switch_runner import (
        _REPLAY_MAX_PASSES,
        EmbeddingSwitchRunError,
    )

    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    monkeypatch.setattr(runner, "_embedding_service", lambda journal: FakeEmbeddingService())

    class EndlessGenerationState:
        def __init__(self) -> None:
            self.sequence = 0

        def watermark(self) -> int:
            self.sequence += 1
            return self.sequence

        def changes_after(
            self, sequence: int, *, up_to: int | None = None
        ) -> list[ProjectionChange]:
            return [ProjectionChange(self.sequence, "memory", "memory-1", False)]

    cast(Any, runner).generation_state = EndlessGenerationState()

    journal = _journal(PHASE_FLIPPING)
    journal.physical_names = {
        "memories": "memories@4096-run",
        "tool_embeddings": "tool_embeddings@4096-run",
        "gobby_github_issues": "gobby_github_issues@4096-run",
    }

    with pytest.raises(EmbeddingSwitchRunError, match="retry once projection writes quiesce"):
        await runner._replay_projection_changes(
            journal, cast(Any, FakeEmbeddingService()), cast(Any, vector_store)
        )
    assert cast(Any, runner.generation_state).sequence == _REPLAY_MAX_PASSES
    assert (
        vector_store.operations
        == [("delete", "memory-1", "memories@4096-run")] * _REPLAY_MAX_PASSES
    )


@pytest.mark.asyncio
async def test_gc_timeout_surfaces_incompatible_acks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ai.embedding_switch import CompletedSwitchRecord
    from gobby.ai.embedding_switch_runner import EmbeddingSwitchRunError

    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase(), gc_wait_seconds=0.05)
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)

    class BlockedGenerationState:
        def __init__(self) -> None:
            self.db_threads: list[int] = []

        def can_collect(self, generation: str, revision: int) -> bool:
            self.db_threads.append(threading.get_ident())
            return False

        def incompatible_serving_acks(self, generation: str, revision: int) -> list[str]:
            self.db_threads.append(threading.get_ident())
            return ["daemon-a (generation=old, revision=1, acknowledged=True)"]

    cast(Any, runner).generation_state = BlockedGenerationState()
    runner._completed_record = CompletedSwitchRecord(
        run_id="4096-run",
        committed_revision=5,
        physical_names={"memories": "memories@4096-run"},
        old_physical_names={"memories": "memories@old"},
        caught_up_watermark=3,
        catalog_key="qwen3-8b-q8",
        target_dim=4096,
        target_model="qwen3-embedding:8b-q8_0",
        target_query_prefix="query:",
        target_api_base=None,
    )

    journal = _journal(PHASE_GC)
    with pytest.raises(EmbeddingSwitchRunError, match="daemon-a"):
        await runner.gc(journal)
    assert journal.phase == PHASE_GC
    assert all(
        thread_id != threading.get_ident()
        for thread_id in cast(Any, runner.generation_state).db_threads
    )


@pytest.mark.asyncio
async def test_gc_abort_request_raises_resumable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ai.embedding_switch import CompletedSwitchRecord
    from gobby.ai.embedding_switch_runner import EmbeddingSwitchRunError

    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    control = EmbeddingSwitchControl()
    runner = EmbeddingSwitchRunner(store, db=FakeDatabase(), control=control, gc_wait_seconds=30.0)
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)

    class BlockedGenerationState:
        def can_collect(self, generation: str, revision: int) -> bool:
            return False

        def incompatible_serving_acks(self, generation: str, revision: int) -> list[str]:
            return []

    cast(Any, runner).generation_state = BlockedGenerationState()
    runner._completed_record = CompletedSwitchRecord(
        run_id="4096-run",
        committed_revision=5,
        physical_names={"memories": "memories@4096-run"},
        old_physical_names={"memories": "memories@old"},
        caught_up_watermark=3,
        catalog_key="qwen3-8b-q8",
        target_dim=4096,
        target_model="qwen3-embedding:8b-q8_0",
        target_query_prefix="query:",
        target_api_base=None,
    )
    control.abort_requested.set()

    with pytest.raises(EmbeddingSwitchRunError, match="aborted"):
        await runner.gc(_journal(PHASE_GC))


@pytest.mark.asyncio
async def test_detect_provider_from_config_refuses_unidentifiable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ai import embedding_switch_runner
    from gobby.config.app import DaemonConfig

    config = DaemonConfig.model_validate({"embeddings": {"api_base": "http://localhost:9321/v1"}})

    async def _no_match(_api_base: str, _api_key: str | None = None, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(embedding_switch_runner, "fingerprint_embedding_server", _no_match)

    with pytest.raises(ValueError, match="--provider"):
        await embedding_switch_runner.detect_provider_from_config(config)


@pytest.mark.asyncio
async def test_detect_provider_from_config_uses_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ai import embedding_switch_runner
    from gobby.config.app import DaemonConfig

    config = DaemonConfig.model_validate({"embeddings": {"api_base": "http://localhost:8323/v1"}})

    async def _vllm(_api_base: str, _api_key: str | None = None, **_kwargs: object) -> str:
        return "vllm"

    monkeypatch.setattr(embedding_switch_runner, "fingerprint_embedding_server", _vllm)

    assert await embedding_switch_runner.detect_provider_from_config(config) == "vllm"
