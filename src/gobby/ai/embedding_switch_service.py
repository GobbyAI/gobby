"""Daemon-owned single-flight lifecycle for embedding collection switches."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
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

logger = logging.getLogger(__name__)

ABORT_WAIT_TIMEOUT_SECONDS = 30.0


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
        config_runtime: Any | None = None,
        runner_factory: RunnerFactory | None = None,
        start_journal: Callable[..., Any] | None = None,
        load_journal: Callable[[], Any | None] | None = None,
    ) -> None:
        self.config_store = config_store
        self.db = db
        self.fence = fence
        self.config_runtime = config_runtime
        self._runner_factory = runner_factory
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

    async def start(
        self,
        catalog_key: str,
        provider: str | None,
        api_base: str | None = None,
    ) -> SwitchOperationStatus:
        async with self._lock:
            self._raise_if_active()
            config = self._active_config()
            provider_name = provider or await detect_provider_from_config(config)
            target_model: str | None = None
            target_api_base: str | None = None
            if provider_name == "vllm":
                target_model, target_api_base = await self._resolve_vllm_target(
                    catalog_key, config, api_base
                )
            journal = self._start_journal(
                self.config_store,
                catalog_key,
                provider_name,
                target_model=target_model,
                target_api_base=target_api_base,
            )
            if isinstance(journal, tuple):
                journal = journal[0]
            return self._launch(journal, "started")

    async def _resolve_vllm_target(
        self,
        catalog_key: str,
        config: Any,
        api_base: str | None,
    ) -> tuple[str, str]:
        """Resolve the served model and pre-flight the dim before any staging."""
        from gobby.agents.local_model import (
            LocalModelError,
            select_vllm_served_model,
            vllm_served_model_ids,
        )
        from gobby.ai.embedding_catalog import get_spec_or_raise
        from gobby.ai.embeddings import EmbeddingGenerationError, EmbeddingService

        configured_api_base = config.embeddings.api_base
        resolved_api_base = api_base or (
            configured_api_base if isinstance(configured_api_base, str) else None
        )
        if not resolved_api_base:
            raise ValueError(
                "vllm embedding switch requires an api_base: pass --api-base or "
                "configure ai.embeddings.api_base"
            )
        spec = get_spec_or_raise(catalog_key)
        api_key = config.embeddings.api_key
        try:
            served = await vllm_served_model_ids(resolved_api_base, api_key)
            target_model = select_vllm_served_model("auto", served, api_base=resolved_api_base)
        except LocalModelError as exc:
            raise ValueError(str(exc)) from exc
        preflight = EmbeddingService(
            model=target_model,
            api_base=resolved_api_base,
            api_key=api_key,
            dim=spec.dim,
        )
        try:
            await preflight.generate_embedding("dim pre-flight", max_retries=1)
        except EmbeddingGenerationError as exc:
            raise ValueError(
                f"vllm embedding pre-flight failed for served model {target_model!r} "
                f"at {resolved_api_base}: {exc}"
            ) from exc
        return target_model, resolved_api_base

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
            if task is None:
                return SwitchOperationStatus(
                    run_id,
                    "failed",
                    "Embedding switch task disappeared before abort",
                )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=ABORT_WAIT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Timed out waiting for embedding switch abort",
                extra={"run_id": run_id},
            )
            return SwitchOperationStatus(
                run_id,
                "timeout",
                "Embedding switch cleanup is still running",
            )
        except Exception as exc:
            logger.exception(
                "Embedding switch abort failed",
                extra={"run_id": run_id},
            )
            return SwitchOperationStatus(run_id, "failed", str(exc))
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
        if self._runner_factory is None:
            runner = _default_runner_factory(
                self.config_store,
                self.db,
                self.control,
                self.fence,
                self.config_runtime,
            )
        else:
            runner = self._runner_factory(self.config_store, self.db, self.control, self.fence)
        run_id = str(journal.run_id)
        self._run_id = run_id
        task = asyncio.create_task(runner.run(journal))
        task.add_done_callback(partial(self._observe_task_result, run_id=run_id))
        self._task = task
        return SwitchOperationStatus(run_id, status, f"Embedding switch {status}")

    def _observe_task_result(self, task: asyncio.Task[Any], *, run_id: str) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info(
                "Embedding switch background task was cancelled",
                extra={"run_id": run_id},
            )
        except Exception:
            logger.exception(
                "Embedding switch background task failed",
                extra={"run_id": run_id},
            )

    def _raise_if_active(self) -> None:
        run_id = self.active_run_id
        if run_id is not None:
            raise EmbeddingSwitchTaskActive(f"Embedding switch {run_id} is already active")

    def _start_default_journal(
        self,
        _store: Any,
        catalog_key: str,
        provider: str,
        *,
        target_model: str | None = None,
        target_api_base: str | None = None,
    ) -> Any:
        config = self._active_config()
        current_dim = config.embeddings.dim
        current_catalog_id = config.embeddings.catalog_id
        current_api_base = config.embeddings.api_base
        journal, _spec = start_switch(
            self.config_store,
            catalog_key,
            provider,
            current_dim=current_dim if isinstance(current_dim, int) else None,
            current_catalog_id=(
                current_catalog_id if isinstance(current_catalog_id, str) else None
            ),
            current_api_base=current_api_base if isinstance(current_api_base, str) else None,
            target_api_base=(
                target_api_base if target_api_base is not None else _provider_api_base(provider)
            ),
            target_model=target_model,
        )
        return journal

    def _active_config(self) -> Any:
        if self.config_runtime is None:
            from gobby.config.app import DaemonConfig

            return DaemonConfig()
        snapshot = self.config_runtime.snapshot
        return (snapshot() if callable(snapshot) else snapshot).active


def _default_runner_factory(
    config_store: Any,
    db: Any,
    control: EmbeddingSwitchControl,
    fence: Any,
    config_runtime: Any | None,
) -> SwitchRunner:
    return EmbeddingSwitchRunner(
        config_store,
        db,
        control=control,
        fence=fence,
        config_runtime=config_runtime,
    )
