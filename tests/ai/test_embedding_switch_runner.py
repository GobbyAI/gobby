"""Tests for the async embedding switch phase runner."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.ai.embedding_switch import (
    PHASE_ABORTED,
    PHASE_ACTIVE,
    PHASE_BUILDING,
    PHASE_FLIPPING,
    PHASE_STAGING,
    SwitchJournal,
    get_switch_status,
)
from gobby.ai.embedding_switch_runner import EmbeddingSwitchRunner
from gobby.ai.embedding_switch_service import EmbeddingSwitchControl
from gobby.config.embedding_keys import AI_EMBEDDING_API_BASE_KEY
from gobby.storage.github_triage import GitHubIssueTriageRecord, GitHubTriageStore

pytestmark = pytest.mark.unit


class FakeConfigStore:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.operations: list[tuple[str, Any]] = []

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any, source: str = "user") -> None:
        self.values[key] = value
        self.operations.append(("set", key, source))

    def set_many(self, entries: dict[str, Any], source: str = "user") -> int:
        self.values.update(entries)
        self.operations.append(("set_many", dict(entries), source))
        return len(entries)

    def set_embedding_switch_values(self, run_id: str, entries: dict[str, Any]) -> int:
        self.values.update(entries)
        self.operations.append(("owner_set", run_id, dict(entries)))
        return len(entries)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.operations.append(("delete", key))


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
    runner = EmbeddingSwitchRunner(FakeConfigStore(), db=object())

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
    runner = EmbeddingSwitchRunner(store, db=object())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)

    _result, journal = await runner.flip(_journal(PHASE_FLIPPING))

    assert journal.phase == PHASE_ACTIVE
    assert store.operations[0][0] == "set"
    assert store.operations[1][0] == "owner_set"
    run_id = store.operations[1][1]
    assert run_id == journal.run_id
    assert store.operations[1][2][AI_EMBEDDING_API_BASE_KEY] is None
    assert journal.old_physical_names["memories"] == "memories@old"
    assert ("alias", "memories@4096-run", "memories") in vector_store.operations
    assert not any(operation[0] == "delete" for operation in vector_store.operations)


@pytest.mark.asyncio
async def test_legacy_bare_collection_flip_deletes_old_bare_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=object())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)

    _result, journal = await runner.flip(_journal(PHASE_FLIPPING))

    assert journal.old_physical_names["memories"] == "memories"
    assert vector_store.operations.index(("alias", "memories@4096-run", "memories")) < (
        vector_store.operations.index(("delete", "memories", None))
    )


@pytest.mark.asyncio
async def test_gc_failure_keeps_active_phase_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore({"memories": "memories@old"})
    runner = EmbeddingSwitchRunner(store, db=object())
    monkeypatch.setattr(runner, "_vector_store", lambda journal: vector_store)
    journal = _journal(PHASE_ACTIVE)
    journal.old_physical_names = {"memories": "memories@old"}

    report = await runner.run(journal)

    assert report.failed is True
    assert report.journal is not None
    assert report.journal.phase == PHASE_ACTIVE
    assert "still targeted by an alias" in (report.journal.error or "")


@pytest.mark.asyncio
async def test_build_uses_target_physical_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=object())
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
async def test_legacy_github_issue_without_source_text_leaves_building_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeConfigStore()
    vector_store = FakeVectorStore()
    runner = EmbeddingSwitchRunner(store, db=object())
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
    runner = EmbeddingSwitchRunner(store, db=object(), control=control)
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
