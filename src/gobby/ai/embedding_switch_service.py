"""Daemon-owned single-flight lifecycle for embedding collection switches."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from gobby.ai.embedding_switch import (
    PHASE_ABORTED,
    PHASE_ACTIVE,
    PHASE_FLIPPING,
    PHASE_GC,
    get_switch_status,
    start_switch,
)
from gobby.ai.embedding_switch_runner import (
    EmbeddingSwitchRunner,
    _provider_api_base,
    detect_provider_from_config,
)
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_CATALOG_KEY,
    AI_EMBEDDING_DIM_KEY,
)


class EmbeddingSwitchTaskActive(RuntimeError):
    """Raised when a second switch attempts to enter the single-flight gate."""


class SwitchRunner(Protocol):
    async def run(self, journal: Any) -> Any: ...


@dataclass(frozen=True)
class SwitchOperationStatus:
    run_id: str | None
    status: str
    message: str


class EmbeddingSwitchControl:
    """Cooperative abort state shared with the active switch runner."""

    def __init__(self) -> None:
        self.abort_requested = asyncio.Event()
        self.flipping_started = False

    def mark_flipping_started(self) -> None:
        self.flipping_started = True


RunnerFactory = Callable[[Any, Any, EmbeddingSwitchControl, Any], SwitchRunner]


class EmbeddingSwitchCoordinator:
    """Own exactly one daemon switch task and expose cooperative lifecycle operations."""

    def __init__(
        self,
        *,
        config_store: Any,
        db: Any,
        fence: Any,
        runner_factory: RunnerFactory | None = None,
        start_journal: Callable[..., Any] | None = None,
        load_journal: Callable[[], Any | None] | None = None,
    ) -> None:
        self.config_store = config_store
        self.db = db
        self.fence = fence
        self._runner_factory = runner_factory or _default_runner_factory
        self._start_journal = start_journal or self._start_default_journal
        self._load_journal = load_journal or (lambda: get_switch_status(config_store))
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None
        self._run_id: str | None = None
        self.control = EmbeddingSwitchControl()

    @property
    def task(self) -> asyncio.Task[Any]:
        if self._task is None:
            raise RuntimeError("No embedding switch task is active")
        return self._task

    @property
    def active_run_id(self) -> str | None:
        if self._task is None or self._task.done():
            return None
        return self._run_id

    async def start(self, catalog_key: str, provider: str | None) -> SwitchOperationStatus:
        async with self._lock:
            self._raise_if_active()
            provider_name = provider or detect_provider_from_config(self.config_store)
            journal = self._start_journal(
                self.config_store,
                catalog_key,
                provider_name,
            )
            if isinstance(journal, tuple):
                journal = journal[0]
            return self._launch(journal, "started")

    async def resume(self) -> SwitchOperationStatus:
        async with self._lock:
            self._raise_if_active()
            journal = self._load_journal()
            if journal is None:
                return SwitchOperationStatus(None, "not_found", "No embedding switch to resume")
            return self._launch(journal, "resumed")

    async def abort(self) -> SwitchOperationStatus:
        async with self._lock:
            if self._task is None or self._task.done():
                journal = self._load_journal()
                if journal is None or str(journal.phase) != PHASE_ABORTED:
                    return SwitchOperationStatus(None, "not_found", "No active embedding switch")
                self._launch(journal, "cleanup_retry")
            run_id = self._run_id
            if self.control.flipping_started:
                return SwitchOperationStatus(
                    run_id,
                    "too_late",
                    "Embedding switch is already flipping and will complete forward",
                )
            self.control.abort_requested.set()
            task = self._task
            assert task is not None
        await asyncio.shield(task)
        result = task.result()
        error = getattr(result, "error", None)
        if error:
            return SwitchOperationStatus(run_id, "failed", str(error))
        return SwitchOperationStatus(run_id, "aborted", "Embedding switch aborted safely")

    def status(self) -> SwitchOperationStatus:
        """Return daemon task state, falling back to the persisted journal."""
        run_id = self.active_run_id
        if run_id is not None:
            return SwitchOperationStatus(run_id, "running", "Embedding switch is running")
        journal = self._load_journal()
        if journal is None:
            return SwitchOperationStatus(None, "not_found", "No embedding switch exists")
        return SwitchOperationStatus(
            str(journal.run_id),
            str(journal.phase),
            "Embedding switch is persisted and idle",
        )

    def _launch(self, journal: Any, status: str) -> SwitchOperationStatus:
        self.control = EmbeddingSwitchControl()
        if str(journal.phase) in (PHASE_FLIPPING, PHASE_ACTIVE, PHASE_GC):
            self.control.mark_flipping_started()
        runner = self._runner_factory(self.config_store, self.db, self.control, self.fence)
        self._run_id = str(journal.run_id)
        self._task = asyncio.create_task(runner.run(journal))
        return SwitchOperationStatus(self._run_id, status, f"Embedding switch {status}")

    def _raise_if_active(self) -> None:
        run_id = self.active_run_id
        if run_id is not None:
            raise EmbeddingSwitchTaskActive(f"Embedding switch {run_id} is already active")

    def _start_default_journal(self, _store: Any, catalog_key: str, provider: str) -> Any:
        current_dim = self.config_store.get(AI_EMBEDDING_DIM_KEY)
        current_catalog_id = self.config_store.get(AI_EMBEDDING_CATALOG_KEY)
        current_api_base = self.config_store.get(AI_EMBEDDING_API_BASE_KEY)
        journal, _spec = start_switch(
            self.config_store,
            catalog_key,
            provider,
            current_dim=current_dim if isinstance(current_dim, int) else None,
            current_catalog_id=(
                current_catalog_id if isinstance(current_catalog_id, str) else None
            ),
            current_api_base=current_api_base if isinstance(current_api_base, str) else None,
            target_api_base=_provider_api_base(provider),
        )
        return journal


def _default_runner_factory(
    config_store: Any,
    db: Any,
    control: EmbeddingSwitchControl,
    fence: Any,
) -> SwitchRunner:
    return EmbeddingSwitchRunner(config_store, db, control=control, fence=fence)
