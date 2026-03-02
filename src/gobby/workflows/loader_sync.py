"""Synchronous wrappers for WorkflowLoader async methods.

Provides WorkflowLoaderSyncMixin for CLI / startup contexts without a running loop.
Extracted from loader.py as part of Strangler Fig decomposition (Wave 2).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from .definitions import PipelineDefinition, WorkflowDefinition
    from .loader_cache import DiscoveredWorkflow

_T = TypeVar("_T")


@runtime_checkable
class _WorkflowLoaderProtocol(Protocol):
    """Protocol declaring async methods that WorkflowLoaderSyncMixin wraps."""

    async def load_workflow(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> WorkflowDefinition | PipelineDefinition | None: ...

    async def load_pipeline(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> PipelineDefinition | None: ...

    async def discover_workflows(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]: ...

    async def discover_pipeline_workflows(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]: ...

    async def validate_workflow_for_agent(
        self,
        workflow_name: str,
        project_path: Path | str | None = None,
    ) -> tuple[bool, str | None]: ...


class WorkflowLoaderSyncMixin:
    """Mixin providing synchronous wrappers for async WorkflowLoader methods."""

    _sync_executor: concurrent.futures.ThreadPoolExecutor | None = None
    _sync_executor_lock: threading.Lock = threading.Lock()

    @classmethod
    def _get_sync_executor(cls) -> concurrent.futures.ThreadPoolExecutor:
        if cls._sync_executor is None:
            with cls._sync_executor_lock:
                if cls._sync_executor is None:
                    cls._sync_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        return cls._sync_executor

    @classmethod
    def shutdown_sync_executor(cls) -> None:
        """Shut down the shared ThreadPoolExecutor, if one was created."""
        with cls._sync_executor_lock:
            if cls._sync_executor is not None:
                cls._sync_executor.shutdown(wait=False)
                cls._sync_executor = None

    @staticmethod
    def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
        """Run a coroutine synchronously, handling both loop and no-loop contexts."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            # No event loop running - safe to use asyncio.run()
            return asyncio.run(coro)

        if threading.current_thread() is threading.main_thread():
            # Same-thread with running loop - offload to a new thread
            # to avoid deadlocking the current loop.
            pool = WorkflowLoaderSyncMixin._get_sync_executor()
            return pool.submit(asyncio.run, coro).result()

        # Worker thread with loop running elsewhere - schedule on existing loop
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    @property
    def _async_self(self) -> _WorkflowLoaderProtocol:
        """Cast self to the protocol so mypy knows the async methods exist."""
        return self  # type: ignore[return-value]

    def load_workflow_sync(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> WorkflowDefinition | PipelineDefinition | None:
        return self._run_sync(
            self._async_self.load_workflow(name, project_path, _inheritance_chain)
        )

    def load_pipeline_sync(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> PipelineDefinition | None:
        return self._run_sync(
            self._async_self.load_pipeline(name, project_path, _inheritance_chain)
        )

    def discover_workflows_sync(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]:
        return self._run_sync(self._async_self.discover_workflows(project_path))

    def discover_pipeline_workflows_sync(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]:
        return self._run_sync(self._async_self.discover_pipeline_workflows(project_path))

    def validate_workflow_for_agent_sync(
        self,
        workflow_name: str,
        project_path: Path | str | None = None,
    ) -> tuple[bool, str | None]:
        return self._run_sync(
            self._async_self.validate_workflow_for_agent(workflow_name, project_path)
        )
