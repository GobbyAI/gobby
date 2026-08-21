from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import gobby.ai.embedding_switch_runner as embedding_switch_runner
import gobby.ai.embedding_switch_service as embedding_switch_service
from gobby.ai.embedding_switch import (
    PHASE_FLIPPING,
    CompletedSwitchRecord,
    SwitchJournal,
    SwitchJournalStateError,
    complete_switch,
    load_completed_switch,
    managed_embedding_projection,
)
from gobby.ai.embedding_switch_runner import EmbeddingSwitchRunner
from gobby.ai.embedding_switch_service import (
    EmbeddingSwitchCoordinator,
    EmbeddingSwitchTaskActive,
)
from gobby.config.embedding_keys import (
    AI_EMBEDDING_CATALOG_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    EMBEDDING_SWITCH_COMPLETED_KEY,
    EMBEDDING_SWITCH_JOURNAL_KEY,
)
from gobby.config.runtime import ConfigRuntime
from gobby.memory.vectorstore import VectorStore
from gobby.runner_init.services import _managed_embedding_collection
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import ConfigStore
from gobby.storage.embedding_generation_state import (
    EmbeddingGenerationLeaseLost,
    EmbeddingGenerationLeaseRenewTransient,
    EmbeddingGenerationNotCaughtUp,
    EmbeddingGenerationState,
    ProjectionChange,
    managed_projection_targets,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import PERSONAL_PROJECT_ID


@pytest.fixture(scope="session", autouse=True)
def _worktree_gdaemon() -> Iterator[None]:
    patch = pytest.MonkeyPatch()
    binary = Path.cwd() / "target" / "debug" / "gdaemon"
    patch.setattr(
        "gobby.storage.schema_contract.resolve_native_bin",
        lambda name: str(binary) if name == "gdaemon" else None,
    )
    yield
    patch.undo()


@dataclass
class FakeJournal:
    run_id: str
    phase: str = "staging"


class FakeRunner:
    def __init__(self, control: Any, events: list[str]) -> None:
        self.control = control
        self.events = events
        self.started = asyncio.Event()
        self.persistent_operation_done = asyncio.Event()

    async def run(self, journal: FakeJournal) -> dict[str, Any]:
        self.events.append(f"run:{journal.run_id}")
        self.started.set()
        await self.control.abort_requested.wait()
        await self.persistent_operation_done.wait()
        self.events.extend(["cleanup:staged", "journal:delete"])
        return {"completed": False, "aborted": True}


def _fake_coordinator(
    *,
    start_journal: FakeJournal | None = None,
    load_journal: FakeJournal | None = None,
) -> tuple[EmbeddingSwitchCoordinator, list[FakeRunner], list[str]]:
    events: list[str] = []
    runners: list[FakeRunner] = []

    def factory(_store: Any, _db: Any, control: Any, _fence: Any) -> FakeRunner:
        runner = FakeRunner(control, events)
        runners.append(runner)
        return runner

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=factory,
        start_journal=(
            (lambda *_args, **_kwargs: start_journal) if start_journal is not None else None
        ),
        load_journal=(lambda: load_journal) if load_journal is not None else None,
    )
    return coordinator, runners, events


@pytest.mark.asyncio
async def test_start_is_single_flight_and_abort_waits_for_cooperative_cleanup() -> None:
    coordinator, runners, events = _fake_coordinator(start_journal=FakeJournal("run-1"))
    first_status = await coordinator.start("catalog", "provider")
    first = coordinator.task
    assert first_status.run_id == "run-1"
    await runners[0].started.wait()

    with pytest.raises(EmbeddingSwitchTaskActive):
        await coordinator.start("catalog", "provider")

    abort_task = asyncio.create_task(coordinator.abort())
    await coordinator.control.abort_requested.wait()
    assert not abort_task.done()

    runners[0].persistent_operation_done.set()
    result = await abort_task
    assert result.status == "aborted"
    assert events == ["run:run-1", "cleanup:staged", "journal:delete"]
    assert (await first).get("aborted") is True
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_abort_is_too_late_once_flipping_has_started() -> None:
    coordinator, runners, _events = _fake_coordinator(start_journal=FakeJournal("run-1"))
    await coordinator.start("catalog", "provider")
    task = coordinator.task
    await runners[0].started.wait()
    coordinator.control.mark_flipping_started()

    result = await coordinator.abort()
    assert result.status == "too_late"
    assert not task.done()

    coordinator.control.abort_requested.set()
    runners[0].persistent_operation_done.set()
    terminal_result = await task
    assert terminal_result == {"completed": False, "aborted": True}
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_resume_uses_the_same_single_flight_gate() -> None:
    coordinator, runners, events = _fake_coordinator(load_journal=FakeJournal("persisted-run"))
    status = await coordinator.resume()
    task = coordinator.task
    assert status.run_id == "persisted-run"
    await runners[0].started.wait()

    with pytest.raises(EmbeddingSwitchTaskActive):
        await coordinator.resume()

    coordinator.control.abort_requested.set()
    runners[0].persistent_operation_done.set()
    terminal_result = await task
    assert terminal_result == {"completed": False, "aborted": True}
    assert events[0] == "run:persisted-run"
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_abort_retries_persisted_cleanup_pending_journal() -> None:
    cleanup_runs: list[str] = []

    class CleanupRunner:
        async def run(self, journal: FakeJournal) -> dict[str, Any]:
            cleanup_runs.append(journal.run_id)
            return {"aborted": True}

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda *_args: CleanupRunner(),
        load_journal=lambda: FakeJournal("cleanup-run", phase="aborted"),
    )

    result = await coordinator.abort()

    assert result.status == "aborted"
    assert result.run_id == "cleanup-run"
    assert cleanup_runs == ["cleanup-run"]
    assert coordinator.active_run_id is None


def _managed_journal() -> SwitchJournal:
    return SwitchJournal(
        run_id="4096-run",
        catalog_key="qwen3-8b-q8",
        target_dim=4096,
        target_model="qwen3-embedding:8b-q8_0",
        target_query_prefix="query:",
        target_api_base=None,
        provider="ollama",
        phase=PHASE_FLIPPING,
        started_at="2026-06-29T00:00:00Z",
        updated_at="2026-06-29T00:00:00Z",
        physical_names={
            "memories": "memories@4096-run",
            "tool_embeddings": "tool_embeddings@4096-run",
            "gobby_github_issues": "gobby_github_issues@4096-run",
        },
        caught_up_watermark=7,
    )


def _complete_managed_switch(store: ConfigStore) -> tuple[SwitchJournal, int]:
    journal = _managed_journal()
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_dict())
    record = complete_switch(store, journal)
    return journal, record.committed_revision


def test_switch_commit_is_one_revision(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)
    journal = _managed_journal()
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_dict())
    before = store.read_snapshot().revision

    committed_revision = complete_switch(store, journal).committed_revision

    snapshot = store.read_snapshot()
    assert committed_revision == before + 1 == snapshot.revision
    assert {
        snapshot.row_revisions[AI_EMBEDDING_CATALOG_KEY],
        snapshot.row_revisions[AI_EMBEDDING_DIM_KEY],
        snapshot.row_revisions[AI_EMBEDDING_MODEL_KEY],
    } == {committed_revision}
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is None
    assert load_completed_switch(store).physical_names == journal.physical_names


@pytest.mark.asyncio
async def test_switch_recovery_uses_runtime_snapshot(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ConfigRepository(temp_db)
    snapshot = repository.read()
    config = repository.runtime_candidate(dict(snapshot.overrides), snapshot.secret_bindings)

    class Runtime:
        def __init__(self) -> None:
            self.calls = 0

        def snapshot(self) -> Any:
            self.calls += 1
            return type("Snapshot", (), {"active": config})()

    runtime = Runtime()
    monkeypatch.setattr(
        embedding_switch_runner,
        "DaemonConfig",
        lambda: pytest.fail("switch recovery rebuilt configuration"),
    )
    runner = EmbeddingSwitchRunner(ConfigStore(temp_db), temp_db, config_runtime=runtime)

    assert runner._runtime_config() is config
    assert runtime.calls == 1

    captured: list[Any] = []

    class DoneRunner:
        async def run(self, _journal: FakeJournal) -> None:
            return None

    def default_factory(
        _store: Any,
        _db: Any,
        _control: Any,
        _fence: Any,
        config_runtime: Any,
    ) -> DoneRunner:
        captured.append(config_runtime)
        return DoneRunner()

    monkeypatch.setattr(embedding_switch_service, "_default_runner_factory", default_factory)
    coordinator = EmbeddingSwitchCoordinator(
        config_store=ConfigStore(temp_db),
        db=temp_db,
        fence=None,
        config_runtime=runtime,
        load_journal=lambda: FakeJournal("runtime-run"),
    )
    await coordinator.resume()
    await coordinator.task
    assert captured == [runtime]


@pytest.mark.asyncio
async def test_managed_revision_converges_across_runtimes(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)
    runtimes = (
        ConfigRuntime(
            ConfigRepository(temp_db),
            managed_resolver=managed_embedding_projection,
        ),
        ConfigRuntime(
            ConfigRepository(temp_db),
            managed_resolver=managed_embedding_projection,
        ),
    )
    try:
        await asyncio.gather(*(runtime.start() for runtime in runtimes))
        journal, revision = _complete_managed_switch(store)
        await asyncio.gather(*(runtime.reconcile_revision(revision) for runtime in runtimes))

        resolved = [
            runtime.capture().managed[EMBEDDING_SWITCH_COMPLETED_KEY] for runtime in runtimes
        ]
        completed = resolved[0]
        assert isinstance(completed, CompletedSwitchRecord)
        assert completed == resolved[1]
        assert completed.physical_names == journal.physical_names
        assert (
            _managed_embedding_collection(
                runtimes[0].capture().managed,
                "memories",
            )
            == "memories@4096-run"
        )
    finally:
        await asyncio.gather(*(runtime.close() for runtime in runtimes))


@pytest.mark.asyncio
async def test_fresh_runtime_start_surfaces_journal_projection_targets(
    temp_db: HubDatabase,
) -> None:
    """A daemon starting mid-switch must project into the journal's physical
    collections from its in-memory managed mapping alone."""
    store = ConfigStore(temp_db)
    journal = _managed_journal()
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_dict())

    runtime = ConfigRuntime(
        ConfigRepository(temp_db),
        managed_resolver=managed_embedding_projection,
    )
    try:
        await runtime.start()
        managed = runtime.capture().managed
        assert managed[EMBEDDING_SWITCH_JOURNAL_KEY] == journal.physical_names
        assert managed_projection_targets(managed, "memory", "memories") == (
            "memories",
            "memories@4096-run",
        )
        assert managed_projection_targets(managed, "tool", "tool_embeddings") == (
            "tool_embeddings",
            "tool_embeddings@4096-run",
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_remote_catchup_after_journal_gc(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)
    journal = _managed_journal()
    journal.target_model = "nomic-embed-text"
    journal.target_dim = 768
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_dict())
    complete_switch(store, journal)
    completed_snapshot = store.read_snapshot()
    assert AI_EMBEDDING_MODEL_KEY not in completed_snapshot.row_revisions
    assert AI_EMBEDDING_DIM_KEY not in completed_snapshot.row_revisions
    runtime = ConfigRuntime(
        ConfigRepository(temp_db),
        managed_resolver=managed_embedding_projection,
    )
    try:
        await runtime.start()
        recovered = runtime.capture().managed[EMBEDDING_SWITCH_COMPLETED_KEY]

        assert isinstance(recovered, CompletedSwitchRecord)
        assert recovered == load_completed_switch(ConfigStore(temp_db))
        assert recovered.run_id == "4096-run"
        assert recovered.physical_names["memories"] == "memories@4096-run"
    finally:
        await runtime.close()


def test_managed_projection_rejects_rows_that_postdate_completion(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    _journal, committed_revision = _complete_managed_switch(store)
    snapshot = store.read_snapshot()
    postdated = SimpleNamespace(
        revision=committed_revision + 1,
        values=snapshot.values,
        overrides=snapshot.overrides,
        row_revisions={
            **snapshot.row_revisions,
            AI_EMBEDDING_MODEL_KEY: committed_revision + 1,
        },
    )

    with pytest.raises(SwitchJournalStateError, match="rows postdate the commit"):
        managed_embedding_projection(postdated)


@pytest.mark.asyncio
async def test_generation_gc_waits_for_acknowledgements(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EmbeddingGenerationState(temp_db)
    first, second = uuid4(), uuid4()
    state.acknowledge(first, "old", 1, lease_seconds=30, acknowledged=False)
    state.acknowledge(second, "old", 1, lease_seconds=30, acknowledged=False)

    state.acknowledge(
        first, "new", 2, lease_seconds=30, caught_up_watermark=0, required_watermark=0
    )
    assert not state.can_collect("new", 2)
    state.acknowledge(
        second, "new", 2, lease_seconds=30, caught_up_watermark=0, required_watermark=0
    )
    assert state.can_collect("new", 2)

    prepared_lease = state.prepare_serving_lease(
        first,
        "new",
        2,
        lease_seconds=30,
        caught_up_watermark=4,
        required_watermark=4,
    )
    with pytest.raises(EmbeddingGenerationLeaseLost, match="activated"):
        prepared_lease.assert_serving()
    prepared_lease.activate()
    prepared_lease.assert_serving()

    lease = state.prepare_serving_lease(
        first,
        "new",
        2,
        lease_seconds=30,
        caught_up_watermark=4,
        required_watermark=4,
    )
    lease.activate()

    def fail_renew_transient(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(state, "renew", fail_renew_transient)
    with pytest.raises(EmbeddingGenerationLeaseRenewTransient):
        lease.renew()
    lease.assert_serving()

    def fail_renew_lost(*_args: object, **_kwargs: object) -> None:
        raise EmbeddingGenerationLeaseLost("Embedding generation lease no longer matches")

    monkeypatch.setattr(state, "renew", fail_renew_lost)
    with pytest.raises(EmbeddingGenerationLeaseLost, match="no longer matches"):
        lease.renew()
    with pytest.raises(EmbeddingGenerationLeaseLost, match="fenced"):
        lease.assert_serving()
    vector_store = VectorStore(serving_guard=lease.assert_serving)
    with pytest.raises(EmbeddingGenerationLeaseLost, match="fenced"):
        await vector_store._call_client(object(), "count")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_write_catchup_replays_into_promoted_generation(temp_db: HubDatabase) -> None:
    state = EmbeddingGenerationState(temp_db)
    store = ConfigStore(temp_db)
    journal = _managed_journal()
    journal.phase = "building"
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_dict())
    assert state.projection_targets("memory", "memories@old") == (
        "memories@old",
        "memories@4096-run",
    )
    state.append_change("memory", "memory-1")
    enumeration_watermark = state.watermark()
    state.append_change("tool", "tool-2")
    state.append_change("memory", "memory-1", is_tombstone=True)
    projected: list[tuple[str, str, bool]] = []

    changes = state.changes_after(enumeration_watermark)
    for change in changes:
        projected.append((change.source_kind, change.source_id, change.is_tombstone))
    caught_up = changes[-1].sequence

    assert projected == [
        ("tool", "tool-2", False),
        ("memory", "memory-1", True),
    ]
    with pytest.raises(EmbeddingGenerationNotCaughtUp):
        state.acknowledge(
            uuid4(),
            "new",
            2,
            lease_seconds=30,
            caught_up_watermark=enumeration_watermark,
            required_watermark=caught_up,
        )
    state.acknowledge(
        uuid4(),
        "new",
        2,
        lease_seconds=30,
        caught_up_watermark=caught_up,
        required_watermark=caught_up,
    )
    complete_switch(store, journal)
    assert state.projection_targets("memory", "memories@old") == (
        "memories@old",
        "memories@4096-run",
    )
    vector_store = VectorStore(generation_state=state)
    vector_store._queries = AsyncMock()
    await vector_store.upsert("memory-2", [0.1], collection_name="memories@old")
    assert [call.args[-1] for call in vector_store._queries.upsert.await_args_list] == [
        "memories@old",
        "memories@4096-run",
    ]

    before_source_write = state.watermark()
    memory_store = LocalMemoryManager(temp_db)
    memory = memory_store.create_memory("projection source", PERSONAL_PROJECT_ID)
    assert state.changes_after(before_source_write)[-1] == ProjectionChange(
        sequence=state.watermark(),
        source_kind="memory",
        source_id=memory.id,
        is_tombstone=False,
    )
    assert memory_store.delete_memory(memory.id)
    assert state.changes_after(before_source_write)[-1].is_tombstone


@pytest.mark.asyncio
async def test_abort_timeout_leaves_cooperative_cleanup_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_switch_service, "ABORT_WAIT_TIMEOUT_SECONDS", 0.01)
    coordinator, runners, _events = _fake_coordinator(start_journal=FakeJournal("run-1"))
    await coordinator.start("catalog", "provider")
    task = coordinator.task
    await runners[0].started.wait()

    result = await coordinator.abort()

    assert result.status == "timeout"
    assert result.run_id == "run-1"
    assert not task.done()
    runners[0].persistent_operation_done.set()
    assert await task == {"completed": False, "aborted": True}
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_abort_translates_runner_failure_to_terminal_status() -> None:
    class FailingRunner:
        def __init__(self, control: Any) -> None:
            self.control = control

        async def run(self, _journal: FakeJournal) -> None:
            await self.control.abort_requested.wait()
            raise RuntimeError("cleanup exploded")

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda _store, _db, control, _fence: FailingRunner(control),
        start_journal=lambda *_args, **_kwargs: FakeJournal("run-1"),
    )
    await coordinator.start("catalog", "provider")

    result = await coordinator.abort()

    assert result.status == "failed"
    assert result.run_id == "run-1"
    assert result.message == "cleanup exploded"
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_background_failure_is_observed_with_run_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingRunner:
        async def run(self, _journal: FakeJournal) -> None:
            raise RuntimeError("background exploded")

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda *_args: FailingRunner(),
        start_journal=lambda *_args, **_kwargs: FakeJournal("run-1"),
    )
    callback_observed = asyncio.Event()

    def mark_callback_observed(_task: asyncio.Task[Any]) -> None:
        callback_observed.set()

    with caplog.at_level(logging.ERROR, logger="gobby.ai.embedding_switch_service"):
        await coordinator.start("catalog", "provider")
        coordinator.task.add_done_callback(mark_callback_observed)
        await asyncio.wait_for(callback_observed.wait(), timeout=1.0)

    assert coordinator.task.done()
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "Embedding switch background task failed"
    ]
    assert len(records) == 1
    assert getattr(records[0], "run_id", None) == "run-1"


def test_managed_projection_survives_post_switch_mutation(temp_db: HubDatabase) -> None:
    """B1 regression: a benign config mutation after switch completion must not
    invalidate the completed-record verification."""
    store = ConfigStore(temp_db)
    _journal, revision = _complete_managed_switch(store)

    ConfigMutations(temp_db).patch(
        expected_revision=revision,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )

    snapshot = store.read_snapshot()
    assert snapshot.revision == revision + 1
    projected = managed_embedding_projection(snapshot)
    completed = projected[EMBEDDING_SWITCH_COMPLETED_KEY]
    assert isinstance(completed, CompletedSwitchRecord)
    assert completed.committed_revision == revision


def test_managed_projection_accepts_unchanged_structural_row_revisions(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    mutations = ConfigMutations(temp_db)
    mutations.patch_internal(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                AI_EMBEDDING_CATALOG_KEY: "qwen3-8b-q8",
                AI_EMBEDDING_DIM_KEY: 4096,
            }
        ),
        source="install",
    )
    structural_revision = store.read_snapshot().revision
    _journal, committed_revision = _complete_managed_switch(store)

    mutations.patch(
        expected_revision=committed_revision,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )

    snapshot = store.read_snapshot()
    assert snapshot.row_revisions[AI_EMBEDDING_CATALOG_KEY] == structural_revision
    assert snapshot.row_revisions[AI_EMBEDDING_DIM_KEY] == structural_revision
    assert structural_revision < committed_revision < snapshot.revision
    completed = managed_embedding_projection(snapshot)[EMBEDDING_SWITCH_COMPLETED_KEY]
    assert isinstance(completed, CompletedSwitchRecord)
    assert completed.committed_revision == committed_revision


def _vllm_switch_coordinator(
    start_journal_calls: list[dict[str, Any]],
) -> EmbeddingSwitchCoordinator:
    def start_journal(_store: Any, catalog_key: str, provider: str, **kwargs: Any) -> FakeJournal:
        start_journal_calls.append({"catalog_key": catalog_key, "provider": provider, **kwargs})
        return FakeJournal("run-vllm")

    class _IdleRunner:
        async def run(self, _journal: Any) -> dict[str, Any]:
            return {"completed": True}

    config = SimpleNamespace(
        embeddings=SimpleNamespace(api_base=None, api_key=None, dim=None, catalog_id=None)
    )
    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda *_args: _IdleRunner(),
        start_journal=start_journal,
        config_runtime=SimpleNamespace(snapshot=SimpleNamespace(active=config)),
    )
    return coordinator


@pytest.mark.asyncio
async def test_start_vllm_resolves_served_model_before_opening_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_journal_calls: list[dict[str, Any]] = []
    coordinator = _vllm_switch_coordinator(start_journal_calls)

    served_requests: list[tuple[str, str | None]] = []

    async def fake_served(api_base: str, api_key: str | None, **_kwargs: Any) -> list[str]:
        served_requests.append((api_base, api_key))
        return ["Qwen/Qwen3-Embedding-0.6B"]

    monkeypatch.setattr("gobby.agents.local_model.vllm_served_model_ids", fake_served)
    preflight = AsyncMock(return_value=[0.0] * 1024)
    monkeypatch.setattr("gobby.ai.embeddings.EmbeddingService.generate_embedding", preflight)

    status = await coordinator.start("qwen3-0.6b-q8", "vllm", api_base="http://localhost:8323/v1")

    assert status.status == "started"
    assert served_requests == [("http://localhost:8323/v1", None)]
    preflight.assert_awaited_once()
    assert start_journal_calls == [
        {
            "catalog_key": "qwen3-0.6b-q8",
            "provider": "vllm",
            "target_model": "Qwen/Qwen3-Embedding-0.6B",
            "target_api_base": "http://localhost:8323/v1",
        }
    ]
    await coordinator.task


@pytest.mark.asyncio
async def test_start_vllm_rejects_dim_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ai.embeddings import EmbeddingGenerationError

    start_journal_calls: list[dict[str, Any]] = []
    coordinator = _vllm_switch_coordinator(start_journal_calls)

    # Missing api_base: refused before any resolution.
    with pytest.raises(ValueError, match="--api-base"):
        await coordinator.start("qwen3-0.6b-q8", "vllm")

    # Multiple served models: refused naming the ids.
    async def two_models(_api_base: str, _api_key: str | None, **_kwargs: Any) -> list[str]:
        return ["model-a", "model-b"]

    monkeypatch.setattr("gobby.agents.local_model.vllm_served_model_ids", two_models)
    with pytest.raises(ValueError, match="model-a.*model-b"):
        await coordinator.start("qwen3-0.6b-q8", "vllm", api_base="http://localhost:8323/v1")

    # Dim mismatch: the pre-flight embedding call fails before staging.
    async def one_model(_api_base: str, _api_key: str | None, **_kwargs: Any) -> list[str]:
        return ["Qwen/Qwen3-Embedding-0.6B"]

    monkeypatch.setattr("gobby.agents.local_model.vllm_served_model_ids", one_model)
    monkeypatch.setattr(
        "gobby.ai.embeddings.EmbeddingService.generate_embedding",
        AsyncMock(
            side_effect=EmbeddingGenerationError(
                "Embedding dimension mismatch for model=Qwen/Qwen3-Embedding-0.6B: "
                "expected 1024, got 4096"
            )
        ),
    )
    with pytest.raises(ValueError, match="pre-flight.*dimension mismatch"):
        await coordinator.start("qwen3-0.6b-q8", "vllm", api_base="http://localhost:8323/v1")

    assert start_journal_calls == []
