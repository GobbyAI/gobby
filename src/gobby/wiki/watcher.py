from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gobby.config.wiki import DEFAULT_WIKI_IGNORE_GLOBS

logger = logging.getLogger(__name__)


class WikiLocalChangeCoordinator(Protocol):
    async def handle_local_changes(
        self, changed_paths_by_scope: dict[str, list[Path]]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class WikiWatchScope:
    name: str
    root: Path


class WikiWatcher:
    """Debounced polling watcher for local wiki file changes."""

    def __init__(
        self,
        *,
        scopes: list[WikiWatchScope],
        coordinator: WikiLocalChangeCoordinator,
        debounce_interval: float,
        poll_interval: float = 0.25,
        ignore_globs: list[str] | None = None,
    ) -> None:
        self._scopes = [
            WikiWatchScope(name=scope.name, root=scope.root.expanduser().resolve())
            for scope in scopes
        ]
        self._coordinator = coordinator
        self._debounce_interval = debounce_interval
        self._poll_interval = poll_interval
        self._ignore_globs = ignore_globs or list(DEFAULT_WIKI_IGNORE_GLOBS)
        self._pending: dict[str, set[Path]] = {}
        self._pending_since: float | None = None
        self._last_index_time: float | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._snapshots: dict[str, dict[Path, tuple[int, int]]] = {}
        self._snapshots_initialized = False

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        try:
            await self._initialize_snapshots()
            while not self._stop_event.is_set():
                await self._scan_once()
                if self._debounce_elapsed():
                    await self.flush_pending()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False

    async def stop(self) -> None:
        self._stop_event.set()
        await self.flush_pending()

    async def record_change(self, path: Path) -> None:
        scope = self._scope_for_path(path)
        if scope is None or self._ignored(scope, path):
            return
        async with self._lock:
            self._pending.setdefault(scope.name, set()).add(path.expanduser().resolve())
            if self._pending_since is None:
                self._pending_since = time.monotonic()

    async def flush_pending(self) -> dict[str, Any] | None:
        async with self._flush_lock:
            async with self._lock:
                if not self._pending:
                    return None
                pending = {
                    scope: sorted(paths, key=lambda item: item.as_posix())
                    for scope, paths in self._pending.items()
                    if paths
                }

            if not pending:
                return None
            result = await self._coordinator.handle_local_changes(pending)
            async with self._lock:
                for scope, paths in pending.items():
                    current = self._pending.get(scope)
                    if current is None:
                        continue
                    current.difference_update(paths)
                    if not current:
                        self._pending.pop(scope, None)
                if not self._pending:
                    self._pending_since = None
            self._last_index_time = time.time()
            return result

    def health(self) -> dict[str, Any]:
        pending_changes = sum(len(paths) for paths in self._pending.values())
        return {
            "running": self._running,
            "scope_count": len(self._scopes),
            "last_index_time": self._last_index_time,
            "pending_debounce": pending_changes > 0,
            "pending_changes": pending_changes,
        }

    async def _initialize_snapshots(self) -> None:
        async with self._lock:
            if self._snapshots_initialized:
                return
            try:
                snapshots = await asyncio.to_thread(self._snapshot_all_scopes)
            except (OSError, RuntimeError):
                logger.warning("Failed to initialize wiki watcher snapshots", exc_info=True)
                self._snapshots = {}
            else:
                self._snapshots = snapshots
            self._snapshots_initialized = True

    def _snapshot_all_scopes(self) -> dict[str, dict[Path, tuple[int, int]]]:
        return {scope.name: self._snapshot(scope) for scope in self._scopes}

    async def _scan_once(self) -> None:
        for scope in self._scopes:
            previous = self._snapshots.get(scope.name, {})
            try:
                current = self._snapshot(scope)
            except (OSError, RuntimeError):
                logger.warning(
                    "Failed to scan wiki watcher scope %s",
                    scope.name,
                    exc_info=True,
                )
                continue
            changed = {
                path for path, signature in current.items() if previous.get(path) != signature
            }
            changed.update(path for path in previous if path not in current)
            self._snapshots[scope.name] = current
            for path in changed:
                try:
                    await self.record_change(path)
                except (OSError, ValueError, RuntimeError):
                    logger.warning(
                        "Failed to record wiki watcher change for %s",
                        path,
                        exc_info=True,
                    )

    def _debounce_elapsed(self) -> bool:
        return self._pending_since is not None and (
            time.monotonic() - self._pending_since >= self._debounce_interval
        )

    def _snapshot(self, scope: WikiWatchScope) -> dict[Path, tuple[int, int]]:
        try:
            if not scope.root.exists():
                return {}
        except OSError:
            return {}
        snapshot: dict[Path, tuple[int, int]] = {}
        for path in scope.root.rglob("*"):
            try:
                if not path.is_file() or self._ignored(scope, path):
                    continue
                stat = path.stat()
                snapshot[path.resolve()] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
        return snapshot

    def _scope_for_path(self, path: Path) -> WikiWatchScope | None:
        resolved = path.expanduser().resolve()
        matches = [
            scope
            for scope in self._scopes
            if resolved == scope.root or resolved.is_relative_to(scope.root)
        ]
        if not matches:
            return None
        return max(matches, key=lambda scope: len(scope.root.parts))

    def _ignored(self, scope: WikiWatchScope, path: Path) -> bool:
        relative = path.expanduser().resolve().relative_to(scope.root).as_posix()
        return any(_matches_glob(relative, pattern) for pattern in self._ignore_globs)


def _matches_glob(relative_path: str, pattern: str) -> bool:
    normalized = pattern.strip("/")
    if fnmatch.fnmatch(relative_path, normalized):
        return True
    if normalized.endswith("/**"):
        return relative_path.startswith(normalized[:-3].rstrip("/") + "/")
    return False
