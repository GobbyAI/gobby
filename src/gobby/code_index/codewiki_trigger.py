"""Debounced post-commit codewiki refresh trigger."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.gwiki_gateway import GwikiGateway, GwikiGatewayError

logger = logging.getLogger(__name__)

_CONFIG_KEY = "wiki.codewiki_on_commit"
_DEFAULT_OUT_DIR = "gobby-wiki"
_AI_VALUES = {"auto", "daemon", "direct", "off"}


@dataclass(frozen=True)
class CodewikiRefreshRequest:
    root_path: str
    project_id: str | None = None
    out_dir: str | None = None
    ai: str = "auto"


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


def _normalize_ai(value: str | None) -> str:
    ai = (value or "auto").strip().lower()
    if ai not in _AI_VALUES:
        raise ValueError("ai must be one of auto, daemon, direct, off")
    return ai


class CodewikiRefreshTrigger:
    """Debounced async trigger for post-commit codewiki generation and ingest."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        config_store_provider: Callable[[], object | None],
        gcode_gateway_factory: Callable[[], GcodeGateway] = GcodeGateway,
        gwiki_gateway_factory: Callable[[Path], GwikiGateway] | None = None,
        debounce_seconds: float = 2.0,
    ) -> None:
        self._loop = loop
        self._config_store_provider = config_store_provider
        self._gcode_gateway_factory = gcode_gateway_factory
        self._gwiki_gateway_factory = gwiki_gateway_factory or (
            # Background codewiki refresh can ingest many generated docs; keep the
            # longer timeout local to this path instead of widening route defaults.
            lambda root: GwikiGateway(project_root=root, timeout_seconds=120.0)
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
    ) -> bool:
        """Schedule a refresh when wiki.codewiki_on_commit is enabled."""
        if not codewiki_on_commit_enabled(self._config_store_provider()):
            return False

        request = CodewikiRefreshRequest(
            root_path=root_path,
            project_id=project_id,
            out_dir=out_dir,
            ai=_normalize_ai(ai),
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
        root = Path(request.root_path).resolve(strict=False)
        out_dir = self._resolve_out_dir(root, request.out_dir)
        try:
            gcode = self._gcode_gateway_factory()
            gwiki = self._gwiki_gateway_factory(root)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "codewiki refresh gateway construction failed for %s: %s",
                request.project_id or root,
                exc,
            )
            return

        try:
            result = await gcode.codewiki(root, out_dir, ai=request.ai)
            changed_paths = _changed_doc_paths(out_dir, result)
            if changed_paths:
                if not out_dir.is_relative_to(self._default_out_dir(root)):
                    # Default vault state is already tracked; gwiki.index() completes that refresh.
                    for path in changed_paths:
                        await gwiki.ingest_file(path)
                await gwiki.index()
            logger.debug(
                "codewiki refresh completed for %s with %d changed docs",
                root,
                len(changed_paths),
            )
        except asyncio.CancelledError:
            raise
        except (GcodeGatewayError, GwikiGatewayError) as exc:
            logger.warning(
                "codewiki refresh failed for %s: %s",
                request.project_id or root,
                exc,
            )

    @staticmethod
    def _resolve_out_dir(root: Path, out_dir: str | None) -> Path:
        value = out_dir or _DEFAULT_OUT_DIR
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        return path.resolve(strict=False)

    @staticmethod
    def _default_out_dir(root: Path) -> Path:
        return (root / _DEFAULT_OUT_DIR).resolve(strict=False)


def _changed_doc_paths(out_dir: Path, result: dict[str, Any]) -> list[Path]:
    changed = result.get("changed_paths")
    if not isinstance(changed, list):
        return []

    paths: list[Path] = []
    for value in changed:
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if not path.is_absolute():
            path = out_dir / path
        resolved = path.resolve(strict=False)
        if resolved.is_relative_to(out_dir):
            paths.append(resolved)
    return paths
