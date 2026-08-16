"""Thin context object for code index daemon integration.

Holds storage and gcode gateway references for daemon background tasks
(maintenance, sync worker, HTTP routes). Actual indexing and projection work is
handled by gcode (Rust CLI).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.code_index.storage import CodeIndexStorage
from gobby.code_index.sync_breaker import SyncCircuitBreaker
from gobby.config.code_index import CodeIndexConfig

if TYPE_CHECKING:
    from gobby.code_index.maintenance_launch import MaintenanceLaunchFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvalidateStoreOutcome:
    """Outcome for one invalidate store cleanup."""

    store: str
    status: str
    error: str | None = None
    pending_retry: bool = False
    deleted: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.error:
            result["error"] = self.error
        if self.pending_retry:
            result["pending_retry"] = True
        if self.deleted is not None:
            result["deleted"] = self.deleted
        return result


class CodeIndexGraphUnavailable(RuntimeError):
    """Raised when code graph operations are disabled for this context."""


class CodeIndexProjectNotFound(RuntimeError):
    """Raised when a project id cannot be resolved to an indexed project root."""


class CodeIndexContext:
    """Daemon-side context for code index operations.

    Replaces the old CodeIndexer orchestrator — gcode now handles
    parsing, hashing, chunking, and hub writes. This object
    provides access to storage and gcode projection lifecycle operations for
    the sync worker, maintenance loop, and HTTP invalidate endpoint.
    """

    def __init__(
        self,
        storage: CodeIndexStorage,
        gcode_gateway: GcodeGateway | None = None,
        config: CodeIndexConfig | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
        daemon_config_breaker: SyncCircuitBreaker | None = None,
        launch_factory: MaintenanceLaunchFactory | None = None,
    ) -> None:
        self._storage = storage
        self._config = config or CodeIndexConfig()
        self._daemon_config_breaker = daemon_config_breaker or SyncCircuitBreaker(
            name="Gcode daemon-config",
            probe_target="daemon config endpoint",
            operation="daemon-owned gcode work",
            failure_threshold=self._config.sync_worker_breaker_failure_threshold,
            base_backoff_seconds=self._config.sync_worker_breaker_backoff_seconds,
            max_backoff_seconds=self._config.sync_worker_breaker_max_backoff_seconds,
        )
        self._gcode_gateway: GcodeGateway | None = gcode_gateway
        self.launch_factory = launch_factory
        if self._gcode_gateway is None and (
            self._config.graph_enabled or self._config.embedding_enabled
        ):
            try:
                self._gcode_gateway = GcodeGateway()
            except GcodeGatewayError as e:
                logger.warning("gcode gateway unavailable during code-index context init: %s", e)
        self._run_db = run_db

    @property
    def storage(self) -> CodeIndexStorage:
        return self._storage

    @property
    def gcode_gateway(self) -> GcodeGateway | None:
        return self._gcode_gateway

    @property
    def daemon_config_breaker(self) -> SyncCircuitBreaker:
        return self._daemon_config_breaker

    @property
    def config(self) -> CodeIndexConfig:
        return self._config

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run code-index hub work on the daemon DB executor when available."""
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def invalidate(self, project_id: str) -> dict[str, Any]:
        """Remove this machine's selector while retaining shared content and projections."""
        stores = {
            "graph": InvalidateStoreOutcome("graph", "skipped"),
            "vector": InvalidateStoreOutcome("vector", "skipped"),
        }
        counts = await self.run_db(self._storage.delete_project_index, project_id)
        stores["hub"] = InvalidateStoreOutcome("hub", "ok", deleted=counts)

        logger.info("Removed local code index state for project %s", project_id)
        return {
            "status": "ok",
            "project_id": project_id,
            "stores": {store: outcome.to_dict() for store, outcome in stores.items()},
            "failed_stores": [],
        }

    async def graph_overview(self, project_id: str, *, limit: int = 200) -> dict[str, Any]:
        """Return a gcode-owned overview graph for an indexed project."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_overview(root, limit=limit)

    async def graph_file(self, project_id: str, file_path: str) -> dict[str, Any]:
        """Return gcode-owned graph context for one indexed file."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_file(root, file_path)

    async def graph_symbol_neighbors(
        self,
        project_id: str,
        symbol_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return gcode-owned neighbors for one symbol."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_neighbors(root, symbol_id, limit=limit)

    async def graph_blast_radius(
        self,
        project_id: str,
        *,
        symbol_id: str | None = None,
        file_path: str | None = None,
        depth: int = 3,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return gcode-owned transitive impact graph."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_blast_radius(
            root,
            symbol_id=symbol_id,
            file_path=file_path,
            depth=depth,
            limit=limit,
        )

    async def graph_path(
        self,
        project_id: str,
        symbol_a: str,
        symbol_b: str,
        *,
        max_depth: int = 6,
    ) -> dict[str, Any]:
        """Return the gcode-owned shortest call path between two symbols."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.symbol_path(root, symbol_a, symbol_b, max_depth)

    async def clear_graph(self, project_id: str) -> dict[str, Any]:
        """Clear only the gcode-owned code graph projection for one project id."""
        gateway = self._require_graph_enabled()
        return await gateway.graph_clear(project_id)

    async def rebuild_graph(self, project_id: str, limit: int = 10_000) -> dict[str, Any]:
        """Rebuild the gcode-owned code graph projection for one indexed project.

        ``limit`` is a deprecated compatibility parameter. Rebuild now delegates to
        gcode, which replays the full indexed project.
        """
        if limit != 10_000:
            logger.warning(
                "CodeIndexContext.rebuild_graph(limit=%s) is deprecated and ignored",
                limit,
            )
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_rebuild(root)

    async def _graph_project_root(self, project_id: str) -> Path:
        self._require_graph_enabled()
        project = await self.run_db(self._storage.get_project_stats, project_id)
        if project is None or not project.root_path:
            raise CodeIndexProjectNotFound(f"Code index project not found: {project_id}")
        return Path(project.root_path).expanduser()

    def _require_graph_enabled(self) -> GcodeGateway:
        if not self._config.graph_enabled or self._gcode_gateway is None:
            raise CodeIndexGraphUnavailable("Code graph not available")
        return self._gcode_gateway
