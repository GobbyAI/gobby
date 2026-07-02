"""Debounced post-commit codewiki refresh trigger."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import psycopg

from gobby.code_index.codewiki_refresh import (
    CodewikiGatewayConstructionError,
    CodewikiGenerator,
    CodewikiRefreshRequest,
    CodewikiRefreshService,
    WikiIndexer,
    normalize_codewiki_ai,
)
from gobby.code_index.gcode_gateway import GcodeGatewayError
from gobby.gwiki_gateway import GwikiGatewayError

logger = logging.getLogger(__name__)

_CONFIG_KEY = "wiki.codewiki_on_commit"


def codewiki_on_commit_enabled(config_store: object | None) -> bool:
    """Return true when the canonical codewiki-on-commit flag is enabled.

    Reads only the live config_store key ``wiki.codewiki_on_commit``, which is
    canonical after startup (see ``config/wiki.py``). Defaults to off when the
    key is unset, preserving default-off plus live-toggle behavior.
    """
    return _coerce_bool(_config_store_value(config_store))


def _config_store_value(config_store: object | None) -> object | None:
    if config_store is None:
        return None
    getter = getattr(config_store, "get", None)
    if not callable(getter):
        return None
    try:
        value: object = getter(_CONFIG_KEY)
        return value
    except (KeyError, TypeError, ValueError, psycopg.Error) as exc:
        logger.warning("Failed to read %s config: %s", _CONFIG_KEY, exc)
        return None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class CodewikiRefreshTrigger:
    """Debounced async trigger for post-commit codewiki generation and ingest."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        config_store_provider: Callable[[], object | None],
        gcode_gateway_factory: Callable[[], CodewikiGenerator] | None = None,
        gwiki_gateway_factory: Callable[[Path], WikiIndexer] | None = None,
        debounce_seconds: float = 2.0,
        refresh_service: CodewikiRefreshService | None = None,
    ) -> None:
        self._loop = loop
        self._config_store_provider = config_store_provider
        if refresh_service is not None:
            self._refresh_service = refresh_service
        elif gcode_gateway_factory is None:
            self._refresh_service = CodewikiRefreshService(
                gwiki_gateway_factory=gwiki_gateway_factory,
            )
        else:
            self._refresh_service = CodewikiRefreshService(
                gcode_gateway_factory=gcode_gateway_factory,
                gwiki_gateway_factory=gwiki_gateway_factory,
            )
        self._debounce_seconds = debounce_seconds
        self._pending_by_root: dict[str, CodewikiRefreshRequest] = {}
        self._flush_timers_by_root: dict[str, asyncio.TimerHandle] = {}
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._running_roots: set[str] = set()

    def request_refresh(
        self,
        *,
        root_path: str,
        project_id: str | None = None,
        out_dir: str | None = None,
        ai: str | None = None,
        scopes: list[str] | None = None,
    ) -> bool:
        """Schedule a refresh when wiki.codewiki_on_commit is enabled."""
        if not codewiki_on_commit_enabled(self._config_store_provider()):
            return False

        request = CodewikiRefreshRequest(
            root_path=root_path,
            project_id=project_id,
            out_dir=out_dir,
            ai=normalize_codewiki_ai(ai),
            scopes=scopes,
        )
        self._loop.call_soon_threadsafe(self._schedule_request, request)
        return True

    def _schedule_request(self, request: CodewikiRefreshRequest) -> None:
        root_key = self._root_key(request.root_path)
        timer = self._flush_timers_by_root.pop(root_key, None)
        if timer is not None:
            timer.cancel()

        self._pending_by_root[root_key] = request
        self._arm_flush_timer(root_key)

    @staticmethod
    def _root_key(root_path: str) -> str:
        return str(Path(root_path).resolve(strict=False))

    async def _flush(self, root_key: str) -> None:
        self._flush_timers_by_root.pop(root_key, None)
        if root_key in self._running_roots:
            return

        request = self._pending_by_root.pop(root_key, None)
        if request is None:
            return

        self._running_roots.add(root_key)
        try:
            await self._run_refresh(request)
        finally:
            self._running_roots.discard(root_key)
            if root_key in self._pending_by_root and root_key not in self._flush_timers_by_root:
                self._arm_flush_timer(root_key)

    def _arm_flush_timer(self, root_key: str) -> None:
        self._flush_timers_by_root[root_key] = self._loop.call_later(
            self._debounce_seconds,
            self._start_flush,
            root_key,
        )

    def _start_flush(self, root_key: str) -> None:
        task = self._loop.create_task(self._flush(root_key))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_task_done)

    def _flush_task_done(self, task: asyncio.Task[None]) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        finally:
            self._flush_tasks.discard(task)
        if exc is not None:
            logger.error(
                "codewiki refresh flush task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _run_refresh(self, request: CodewikiRefreshRequest) -> None:
        try:
            result = await self._refresh_service.refresh(request)
            logger.debug(
                "codewiki refresh completed for %s with %d changed docs",
                result.root,
                result.changed_count,
            )
        except asyncio.CancelledError:
            raise
        except CodewikiGatewayConstructionError as exc:
            logger.warning("%s", exc)
        except (GcodeGatewayError, GwikiGatewayError) as exc:
            logger.warning(
                "codewiki refresh failed for %s: %s",
                request.project_id or request.root_path,
                exc,
            )
