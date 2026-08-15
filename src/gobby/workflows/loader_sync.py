"""Synchronous wrappers for PipelineLoader async methods.

Provides PipelineLoaderSyncMixin for CLI / startup contexts without a running loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast, runtime_checkable

if TYPE_CHECKING:
    from .loader_cache import DiscoveredWorkflow
    from .pipeline_models import PipelineDefinition

_T = TypeVar("_T")


@runtime_checkable
class _PipelineLoaderProtocol(Protocol):
    """Protocol declaring async methods that PipelineLoaderSyncMixin wraps."""

    async def load_pipeline(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> PipelineDefinition | None: ...

    async def discover_pipelines(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]: ...

    async def validate_pipeline_for_agent(
        self,
        pipeline_name: str,
        project_id: str | None = None,
    ) -> tuple[bool, str | None]: ...


class PipelineLoaderSyncMixin:
    """Mixin providing synchronous wrappers for async PipelineLoader methods."""

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
            return asyncio.run(coro)

        pool = PipelineLoaderSyncMixin._get_sync_executor()
        return pool.submit(asyncio.run, coro).result()

    @property
    def _async_self(self) -> _PipelineLoaderProtocol:
        """Cast self to the protocol so mypy knows the async methods exist."""
        return cast(_PipelineLoaderProtocol, self)

    def load_pipeline_sync(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> PipelineDefinition | None:
        return self._run_sync(
            self._async_self.load_pipeline(name, project_path, _inheritance_chain)
        )

    def discover_pipelines_sync(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]:
        return self._run_sync(self._async_self.discover_pipelines(project_path))

    def validate_pipeline_for_agent_sync(
        self,
        pipeline_name: str,
        project_id: str | None = None,
    ) -> tuple[bool, str | None]:
        return self._run_sync(
            self._async_self.validate_pipeline_for_agent(pipeline_name, project_id)
        )
