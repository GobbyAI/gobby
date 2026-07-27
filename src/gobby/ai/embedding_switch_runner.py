"""Async phase runner for resumable embedding model switches."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from gobby.ai.embedding_switch import (
    PHASE_ABORTED,
    PHASE_ACTIVE,
    PHASE_BUILDING,
    PHASE_FLIPPING,
    PHASE_GC,
    PHASE_STAGING,
    SwitchJournal,
    active_alias_names,
    advance_phase,
    build_physical_names,
    complete_aborted_switch,
    complete_switch,
    persist_journal,
    record_switch_error,
)
from gobby.ai.embeddings import EmbeddingService
from gobby.cli.installers.embedding import (
    _PROVIDER_CONFIG,
    _probe_embedding_dim,
    _semantic_smoke_test,
    _setup_lmstudio,
    _setup_ollama,
)
from gobby.config.app import load_config
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_CATALOG_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
)
from gobby.github_triage.issue_index import issue_point_id
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.semantic_search import SemanticToolSearch
from gobby.memory.vectorstore import VectorStore
from gobby.projects.write_fence import ProjectWriteRejected
from gobby.storage.github_triage import GitHubIssueTriageRecord, GitHubTriageStore
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import LocalProjectManager

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100


class EmbeddingSwitchRunError(RuntimeError):
    """Raised when a switch phase records a resumable journal error."""


class MissingGitHubIssueSourceTextError(EmbeddingSwitchRunError):
    """Raised when legacy GitHub issue rows cannot be re-embedded safely."""


class EmbeddingSwitchAbortRequested(Exception):
    """Internal cooperative checkpoint used before alias flipping begins."""


class SwitchControlProtocol(Protocol):
    abort_requested: asyncio.Event
    flipping_started: bool

    def mark_flipping_started(self) -> None: ...


class ProjectFenceProtocol(Protocol):
    def writer(self, project_id: str) -> AbstractAsyncContextManager[None]: ...


class _NoopSwitchControl:
    def __init__(self) -> None:
        self.abort_requested = asyncio.Event()
        self.flipping_started = False

    def mark_flipping_started(self) -> None:
        self.flipping_started = True


@dataclass(frozen=True)
class PhaseResult:
    """Short summary for a completed switch phase."""

    phase: str
    message: str
    count: int | None = None


@dataclass
class SwitchRunReport:
    """Result returned to the daemon coordinator after running switch phases."""

    journal: SwitchJournal | None
    phase_results: list[PhaseResult] = field(default_factory=list)
    completed: bool = False
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


def detect_provider_from_config(config_store: Any) -> str:
    """Infer the provider from the active API base, matching the previous CLI behavior."""
    current_api_base = config_store.get(AI_EMBEDDING_API_BASE_KEY)
    if isinstance(current_api_base, str):
        if "11434" in current_api_base:
            return "ollama"
        if "1234" in current_api_base:
            return "lmstudio"
    return "ollama"


class EmbeddingSwitchRunner:
    """Run switch phases with journaled failure and resume semantics."""

    def __init__(
        self,
        config_store: Any,
        db: Any,
        *,
        control: SwitchControlProtocol | None = None,
        fence: ProjectFenceProtocol | None = None,
    ) -> None:
        self.config_store = config_store
        self.db = db
        self.control = control or _NoopSwitchControl()
        self.fence = fence

    async def run(self, journal: SwitchJournal) -> SwitchRunReport:
        report = SwitchRunReport(journal=journal)
        while True:
            try:
                if journal.phase in (PHASE_STAGING, PHASE_BUILDING):
                    self._check_abort()
                if journal.phase == PHASE_STAGING:
                    result, journal = await self.stage(journal)
                    report.phase_results.append(result)
                    report.journal = journal
                    continue
                if journal.phase == PHASE_BUILDING:
                    result, journal = await self.build(journal)
                    report.phase_results.append(result)
                    report.journal = journal
                    continue
                if journal.phase == PHASE_FLIPPING:
                    self.control.mark_flipping_started()
                    result, journal = await self.flip(journal)
                    report.phase_results.append(result)
                    report.journal = journal
                    continue
                if journal.phase == PHASE_ACTIVE:
                    result = await self.gc(journal)
                    report.phase_results.append(result)
                    report.completed = True
                    report.journal = None
                    return report
                if journal.phase == PHASE_GC:
                    journal = advance_phase(self.config_store, journal, PHASE_ACTIVE)
                    report.journal = journal
                    continue
                if journal.phase == PHASE_ABORTED:
                    return await self._finish_aborted_cleanup(journal, report)
                report.error = f"Unsupported embedding switch phase: {journal.phase}"
                return report
            except EmbeddingSwitchAbortRequested:
                try:
                    journal = advance_phase(self.config_store, journal, PHASE_ABORTED)
                except Exception as exc:
                    report.error = str(exc)
                    report.journal = self._record_phase_error(journal, exc)
                    return report
                report.journal = journal
                return await self._finish_aborted_cleanup(journal, report)
            except Exception as exc:
                report.error = str(exc)
                report.journal = self._record_phase_error(journal, exc)
                return report

    async def stage(self, journal: SwitchJournal) -> tuple[PhaseResult, SwitchJournal]:
        """Prepare and validate the target provider/model before any index mutation."""
        config = load_config(config_store=self.config_store)
        api_key = config.embeddings.api_key

        if journal.provider == "ollama":
            setup_result = await asyncio.to_thread(_setup_ollama, ollama_tag=journal.target_model)
        elif journal.provider == "lmstudio":
            from gobby.ai.embedding_catalog import get_spec_or_raise

            spec = get_spec_or_raise(journal.catalog_key)
            setup_result = await asyncio.to_thread(
                _setup_lmstudio,
                model_id=journal.target_model,
                gguf_repo=spec.gguf_repo,
                gguf_filename=spec.gguf_filename,
                search_keyword=spec.family,
            )
        else:
            setup_result = {"success": True, "action": "no_provider_setup"}

        if not setup_result.get("success"):
            raise EmbeddingSwitchRunError(str(setup_result.get("error") or "provider setup failed"))

        actual_dim = await asyncio.to_thread(
            _probe_embedding_dim,
            journal.target_model,
            journal.target_api_base,
            api_key,
        )
        if actual_dim != journal.target_dim:
            raise EmbeddingSwitchRunError(
                f"Target model dimension probe failed: expected {journal.target_dim}, "
                f"got {actual_dim or 'unknown'}"
            )

        smoke_ok = await asyncio.to_thread(
            _semantic_smoke_test,
            journal.target_model,
            journal.target_api_base,
            api_key,
            journal.target_query_prefix,
        )
        if not smoke_ok:
            raise EmbeddingSwitchRunError("Target embedding model failed semantic smoke test")

        self._check_abort()
        journal = advance_phase(self.config_store, journal, PHASE_BUILDING)
        return PhaseResult(PHASE_STAGING, "target provider staged and validated"), journal

    async def build(self, journal: SwitchJournal) -> tuple[PhaseResult, SwitchJournal]:
        """Build target physical collections without touching active aliases."""
        vector_store = self._vector_store(journal)
        service = self._embedding_service(journal)
        physical_names = build_physical_names(journal)

        for collection_name in physical_names.values():
            self._check_abort()
            await vector_store.ensure_collection(
                collection_name,
                journal.target_dim,
                recreate_on_mismatch=True,
            )

        count = 0
        count += await self._build_memory_collection(journal, service, vector_store)
        count += await self._build_tool_collection(journal, vector_store)
        count += await self._build_github_issue_collection(journal, service, vector_store)

        self._check_abort()
        journal = advance_phase(self.config_store, journal, PHASE_FLIPPING)
        return PhaseResult(PHASE_BUILDING, "target physical collections built", count), journal

    async def flip(self, journal: SwitchJournal) -> tuple[PhaseResult, SwitchJournal]:
        """Flip aliases to the target physical collections, then write embedding config."""
        vector_store = self._vector_store(journal)
        aliases = await vector_store.get_aliases()
        active_aliases = active_alias_names()
        physical_names = build_physical_names(journal)

        if not journal.old_physical_names:
            journal.old_physical_names = {
                kind: aliases.get(alias_name, alias_name)
                for kind, alias_name in active_aliases.items()
            }
            persist_journal(self.config_store, journal)

        for kind, alias_name in active_aliases.items():
            target_name = physical_names[kind]
            await vector_store.create_alias(target_name, alias_name)
            old_name = journal.old_physical_names.get(kind)
            if old_name == alias_name:
                await _delete_collection_if_present(vector_store, old_name)

        embedding_values = {
            AI_EMBEDDING_MODEL_KEY: journal.target_model,
            AI_EMBEDDING_DIM_KEY: journal.target_dim,
            AI_EMBEDDING_CATALOG_KEY: journal.catalog_key,
            AI_EMBEDDING_QUERY_PREFIX_KEY: journal.target_query_prefix,
            AI_EMBEDDING_API_BASE_KEY: journal.target_api_base,
        }
        self.config_store.set_embedding_switch_values(journal.run_id, embedding_values)

        journal = advance_phase(self.config_store, journal, PHASE_ACTIVE)
        return PhaseResult(PHASE_FLIPPING, "aliases flipped and embedding config written"), journal

    async def gc(self, journal: SwitchJournal) -> PhaseResult:
        """Delete old physical collections recorded before the flip."""
        vector_store = self._vector_store(journal)
        aliases = await vector_store.get_aliases()
        current_targets = set(aliases.values())
        deleted = 0

        for collection_name in set(journal.old_physical_names.values()):
            if not collection_name:
                continue
            if collection_name in current_targets:
                raise EmbeddingSwitchRunError(
                    f"Refusing to delete {collection_name}; it is still targeted by an alias"
                )
            await _delete_collection_if_present(vector_store, collection_name)
            deleted += 1

        complete_switch(self.config_store, journal)
        return PhaseResult(PHASE_GC, "old physical collections garbage-collected", deleted)

    async def _build_memory_collection(
        self,
        journal: SwitchJournal,
        service: EmbeddingService,
        vector_store: VectorStore,
    ) -> int:
        collection_name = build_physical_names(journal)["memories"]
        memory_store = LocalMemoryManager(self.db)
        total = 0
        offset = 0

        while True:
            memories = memory_store.list_memories(limit=_BATCH_SIZE, offset=offset)
            if not memories:
                return total

            texts = [memory.content for memory in memories]
            embeddings = await service.generate_embeddings(texts)
            items_by_project: dict[str, list[tuple[str, list[float], dict[str, Any]]]] = {}
            for memory, embedding in zip(memories, embeddings, strict=True):
                items_by_project.setdefault(memory.project_id, []).append(
                    (
                        memory.id,
                        embedding,
                        {
                            "project_id": memory.project_id,
                            "memory_type": memory.memory_type,
                            "source_type": memory.source_type,
                            "tags": list(memory.tags or []),
                        },
                    )
                )
            embedded_count = 0
            for project_id, items in items_by_project.items():
                self._check_abort()
                try:
                    async with self._writer(project_id):
                        await vector_store.batch_upsert(items, collection_name=collection_name)
                except ProjectWriteRejected:
                    logger.info(
                        "Skipping embedding-switch memory build for unavailable project %s",
                        project_id,
                    )
                    continue
                embedded_count += len(items)
            total += embedded_count
            offset += len(memories)

    async def _build_tool_collection(
        self, journal: SwitchJournal, vector_store: VectorStore
    ) -> int:
        collection_name = build_physical_names(journal)["tool_embeddings"]
        config = load_config(config_store=self.config_store)
        mcp_manager = LocalMCPManager(self.db)
        search = SemanticToolSearch(
            self.db,
            embedding_model=journal.target_model,
            embedding_dim=journal.target_dim,
            embedding_api_key=config.embeddings.api_key,
            api_base=journal.target_api_base,
            vector_store=vector_store,
            collection_name=collection_name,
        )

        count = 0
        for project in LocalProjectManager(self.db).list():
            self._check_abort()
            internal_manager = setup_internal_registries(
                config,
                db=self.db,
                project_id=project.id,
                mcp_manager=mcp_manager,
            )
            try:
                async with self._writer(project.id):
                    stats = await search.embed_all_tools(
                        project.id,
                        mcp_manager,
                        internal_manager=internal_manager,
                    )
            except ProjectWriteRejected:
                logger.info(
                    "Skipping embedding-switch tool build for unavailable project %s",
                    project.id,
                )
                continue
            embedded = stats.get("embedded")
            if isinstance(embedded, int):
                count += embedded
        return count

    async def _build_github_issue_collection(
        self,
        journal: SwitchJournal,
        service: EmbeddingService,
        vector_store: VectorStore,
    ) -> int:
        collection_name = build_physical_names(journal)["gobby_github_issues"]
        issue_store = GitHubTriageStore(self.db)
        total = 0
        offset = 0

        while True:
            records = issue_store.list_issue_records(limit=_BATCH_SIZE, offset=offset)
            if not records:
                return total

            for record in records:
                self._check_abort()
                try:
                    async with self._writer(record.project_id):
                        await self._upsert_github_issue_record(
                            record, service, vector_store, collection_name
                        )
                except ProjectWriteRejected:
                    logger.info(
                        "Skipping embedding-switch issue build for unavailable project %s",
                        record.project_id,
                    )
                    continue
                total += 1
            offset += len(records)

    async def _upsert_github_issue_record(
        self,
        record: GitHubIssueTriageRecord,
        service: EmbeddingService,
        vector_store: VectorStore,
        collection_name: str,
    ) -> None:
        if not record.source_text:
            raise MissingGitHubIssueSourceTextError(
                f"GitHub issue {record.repo}#{record.issue_number} lacks source_text; "
                "run GitHub triage reconcile/reprocessing before resuming embedding switch."
            )

        embedding = await service.generate_embedding(record.source_text)
        payload = {
            "project_id": record.project_id,
            "repo": record.repo,
            "issue_number": record.issue_number,
            "issue_url": record.issue_url,
            "state": record.issue_state,
            "labels": list(record.labels),
            "updated_at": record.issue_updated_at,
            "content_hash": record.content_hash,
            "task_id": record.task_id,
            "dedup_issue_key": record.dedup_issue_key,
            "source_text": record.source_text,
        }
        await vector_store.upsert(
            issue_point_id(record.project_id, record.repo, record.issue_number),
            embedding,
            payload,
            collection_name=collection_name,
        )

    def _vector_store(self, journal: SwitchJournal) -> VectorStore:
        config = load_config(config_store=self.config_store)
        return VectorStore(
            url=config.databases.qdrant.url,
            api_key=config.databases.qdrant.api_key,
            embedding_dim=journal.target_dim,
        )

    def _embedding_service(self, journal: SwitchJournal) -> EmbeddingService:
        config = load_config(config_store=self.config_store)
        return EmbeddingService(
            model=journal.target_model,
            api_base=journal.target_api_base,
            api_key=config.embeddings.api_key,
            dim=journal.target_dim,
            query_prefix=journal.target_query_prefix,
        )

    def _record_phase_error(self, journal: SwitchJournal, exc: Exception) -> SwitchJournal:
        return record_switch_error(self.config_store, journal, str(exc), phase=journal.phase)

    def _check_abort(self) -> None:
        if self.control.abort_requested.is_set() and not self.control.flipping_started:
            raise EmbeddingSwitchAbortRequested

    async def _cleanup_staged_collections(self, journal: SwitchJournal) -> None:
        vector_store = self._vector_store(journal)
        staged_names = tuple(build_physical_names(journal).values())
        aliases = await vector_store.get_aliases()
        protected_names = set(staged_names).intersection(aliases.values())
        if protected_names:
            rendered_names = ", ".join(sorted(protected_names))
            raise EmbeddingSwitchRunError(
                f"Refusing to delete {rendered_names}; it is still targeted by an alias"
            )
        for collection_name in staged_names:
            await _delete_collection_if_present(vector_store, collection_name)

    async def _finish_aborted_cleanup(
        self,
        journal: SwitchJournal,
        report: SwitchRunReport,
    ) -> SwitchRunReport:
        try:
            await self._cleanup_staged_collections(journal)
            complete_aborted_switch(self.config_store, journal)
        except Exception as exc:
            report.error = str(exc)
            report.journal = record_switch_error(
                self.config_store,
                journal,
                str(exc),
                phase=PHASE_ABORTED,
            )
            return report
        report.phase_results.append(
            PhaseResult(PHASE_ABORTED, "staged collections cleaned and switch aborted")
        )
        report.journal = None
        return report

    def _writer(self, project_id: str) -> AbstractAsyncContextManager[None]:
        if self.fence is not None:
            return self.fence.writer(project_id)
        return _noop_writer()


@asynccontextmanager
async def _noop_writer() -> AsyncIterator[None]:
    yield


def _provider_api_base(provider: str) -> str | None:
    if provider == "none":
        raise ValueError("Embedding switch requires an embedding provider.")
    try:
        value = _PROVIDER_CONFIG[provider]["api_base"]
    except KeyError as exc:
        raise ValueError(f"Unknown embedding provider: {provider}") from exc
    return value if isinstance(value, str) else None


async def _delete_collection_if_present(vector_store: VectorStore, collection_name: str) -> None:
    try:
        await vector_store.delete_collection(collection_name)
    except Exception as exc:
        if _is_missing_collection_error(exc):
            logger.debug(
                "Skipping missing embedding collection during switch GC: %s", collection_name
            )
            return
        raise


def _is_missing_collection_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "not found" in message or "does not exist" in message or "doesn't exist" in message
