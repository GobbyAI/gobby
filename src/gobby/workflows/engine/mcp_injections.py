"""Per-session caching for optional MCP-backed rule context."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from gobby.adapters.capabilities import hook_supports_model_context
from gobby.hooks.adapter_execution import release_session_admission_for_external_wait
from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.events import ContextPart, HookEvent
from gobby.hooks.mcp_result import mcp_call_succeeded
from gobby.hooks.receipt_effects import stage_append_set_variables
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.engine._offload import offload
from gobby.workflows.engine.delivery_formatting import (
    DeliveryFormattingMixin,
    _is_empty_inject_payload,
)
from gobby.workflows.reserved_variables import is_internal_rule, is_reserved_workflow_variable

logger = logging.getLogger(__name__)

_CACHED_INJECTION_TOOLS = frozenset(
    {
        ("gobby-memory", "surface_memories"),
        ("gobby-review-learning", "recall_review_lessons_by_class"),
        ("gobby-review-learning", "recall_review_lessons_for_files"),
        ("gobby-skills", "list_hubs"),
    }
)
# Memory recall waits up to this long rather than dropping its result (Josh,
# #22807); it must stay well inside workflow.timeout (24 s) with the other
# turn-start inline calls. #22708 owns the underlying recall slowness.
_INLINE_WAIT_CAP_SECONDS = 10.0
_BACKGROUND_TIMEOUT_SECONDS = 30.0
_CACHE_TTL_SECONDS = 120.0
_MAX_CACHED_SESSIONS = 512
_MAX_CACHED_PER_SESSION = 16


@dataclass(frozen=True, slots=True)
class _CompletedMcpInjection:
    raw_result: Any
    filled_at: float


@dataclass(frozen=True, slots=True)
class _InflightMcpInjection:
    task: asyncio.Task[_CompletedMcpInjection | None]
    deadline: float


@dataclass(frozen=True, slots=True)
class _LateMcpInjection:
    effect: Any
    row: RuleDefinitionRow
    completed: _CompletedMcpInjection


def _put_session_lru[T](
    sessions: OrderedDict[str, OrderedDict[str, T]],
    session_id: str,
    call_key: str,
    value: T,
) -> None:
    session_entries = sessions.pop(session_id, OrderedDict())
    session_entries[call_key] = value
    session_entries.move_to_end(call_key)
    if len(session_entries) > _MAX_CACHED_PER_SESSION:
        session_entries.popitem(last=False)
    sessions[session_id] = session_entries
    if len(sessions) > _MAX_CACHED_SESSIONS:
        sessions.popitem(last=False)


class CachedMcpInjectionMixin(DeliveryFormattingMixin):
    """Share optional MCP dispatches and cache their results by platform session.

    A result whose inline wait timed out is delivered once on the session's next
    hook that carries model context, unless its TTL has passed (#22708).
    """

    db: Any
    _mcp_dispatcher: Any
    _mcp_injection_lock: threading.Lock
    _mcp_injection_inflight: dict[str, _InflightMcpInjection]
    _mcp_injection_cache: OrderedDict[
        str,
        OrderedDict[str, _CompletedMcpInjection],
    ]
    # Call keys whose inline waiter gave up while the dispatch was still running.
    _mcp_injection_abandoned: set[str]
    _mcp_injection_late: OrderedDict[str, OrderedDict[str, _LateMcpInjection]]

    def _initialize_cached_mcp_injections(self) -> None:
        self._mcp_injection_lock = threading.Lock()
        self._mcp_injection_inflight = {}
        self._mcp_injection_cache = OrderedDict()
        self._mcp_injection_abandoned = set()
        self._mcp_injection_late = OrderedDict()

    @staticmethod
    def _uses_mcp_injection_cache(effect: Any) -> bool:
        return bool(
            effect.inject_result
            and not effect.block_on_failure
            and not effect.block_on_success
            and (effect.server, effect.tool) in _CACHED_INJECTION_TOOLS
        )

    @staticmethod
    def _mcp_injection_call_key(
        platform_session_id: str,
        effect: Any,
        arguments: dict[str, Any],
    ) -> str:
        return json.dumps(
            [platform_session_id, effect.server, effect.tool, arguments],
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )

    async def _apply_cached_mcp_injection(
        self,
        effect: Any,
        row: RuleDefinitionRow,
        arguments: dict[str, Any],
        event: HookEvent,
        variables: dict[str, Any],
        context_parts: list[ContextPart],
        staged_variable_updates: dict[str, Any],
    ) -> bool:
        """Apply a cached/shared result, returning False when caching is unavailable."""
        platform_session_id = event.metadata.get("_platform_session_id")
        if not isinstance(platform_session_id, str) or not platform_session_id:
            return False

        call_key = self._mcp_injection_call_key(platform_session_id, effect, arguments)
        completed: _CompletedMcpInjection | None = None
        inflight: _InflightMcpInjection | None = None
        loop = asyncio.get_running_loop()

        with self._mcp_injection_lock:
            session_cache = self._mcp_injection_cache.get(platform_session_id)
            if session_cache is not None:
                completed = session_cache.get(call_key)
                if completed is not None:
                    if loop.time() - completed.filled_at > _CACHE_TTL_SECONDS:
                        del session_cache[call_key]
                        completed = None
                    else:
                        session_cache.move_to_end(call_key)
                        self._mcp_injection_cache.move_to_end(platform_session_id)

            if completed is None:
                inflight = self._mcp_injection_inflight.get(call_key)
                if inflight is None:
                    wait_seconds = min(
                        effect.timeout_seconds or _INLINE_WAIT_CAP_SECONDS,
                        _INLINE_WAIT_CAP_SECONDS,
                    )
                    task = create_background_task(
                        self._run_cached_mcp_injection(
                            effect,
                            row,
                            dict(arguments),
                            event,
                            platform_session_id,
                            call_key,
                        )
                    )
                    inflight = _InflightMcpInjection(
                        task=task,
                        deadline=loop.time() + wait_seconds,
                    )
                    self._mcp_injection_inflight[call_key] = inflight

        if completed is None:
            assert inflight is not None
            release_session_admission_for_external_wait()
            remaining_seconds = inflight.deadline - loop.time()
            if remaining_seconds > 0:
                with contextlib.suppress(TimeoutError):
                    completed = await asyncio.wait_for(
                        asyncio.shield(inflight.task),
                        timeout=remaining_seconds,
                    )
            if completed is None:
                if not inflight.task.done():
                    self._log_inline_timeout(effect, row)
                completed = self._defer_timed_out_injection(
                    platform_session_id,
                    call_key,
                    inflight.task,
                )

        if completed is None:
            return True

        with self._mcp_injection_lock:
            # Delivered inline now, so a queued late copy of this call must not follow.
            late_entries = self._mcp_injection_late.get(platform_session_id)
            if late_entries is not None:
                late_entries.pop(call_key, None)

        self._record_injection_success(effect, row, variables, staged_variable_updates)
        await self._append_injected_mcp_result(
            server=effect.server,
            tool=effect.tool,
            raw_result=completed.raw_result,
            event=event,
            variables=variables,
            context_parts=context_parts,
        )
        return True

    @staticmethod
    def _log_inline_timeout(effect: Any, row: RuleDefinitionRow) -> None:
        wait_seconds = min(
            effect.timeout_seconds or _INLINE_WAIT_CAP_SECONDS,
            _INLINE_WAIT_CAP_SECONDS,
        )
        logger.warning(
            "Inline mcp_call %s/%s timed out after %ss (rule %s); dispatch continues",
            effect.server,
            effect.tool,
            wait_seconds,
            row.name,
        )

    def _defer_timed_out_injection(
        self,
        platform_session_id: str,
        call_key: str,
        task: asyncio.Task[_CompletedMcpInjection | None],
    ) -> _CompletedMcpInjection | None:
        """Return a result that filled after the wait ended, or queue it for late delivery."""
        with self._mcp_injection_lock:
            session_cache = self._mcp_injection_cache.get(platform_session_id)
            completed = session_cache.get(call_key) if session_cache is not None else None
            if completed is None and not task.done():
                self._mcp_injection_abandoned.add(call_key)
            return completed

    @staticmethod
    def _record_injection_success(
        effect: Any,
        row: RuleDefinitionRow,
        variables: dict[str, Any],
        staged_variable_updates: dict[str, Any],
    ) -> None:
        if effect.success_variable and (
            is_internal_rule(row) or not is_reserved_workflow_variable(effect.success_variable)
        ):
            variables[effect.success_variable] = True
            if effect.delivery == "on_receipt":
                staged_variable_updates[effect.success_variable] = True

    async def _deliver_late_mcp_injections(
        self,
        event: HookEvent,
        variables: dict[str, Any],
        context_parts: list[ContextPart],
        staged_variable_updates: dict[str, Any],
    ) -> None:
        """Append timed-out results to this hook once, dropping any past the cache TTL."""
        platform_session_id = event.metadata.get("_platform_session_id")
        if not isinstance(platform_session_id, str) or not platform_session_id:
            return
        if not hook_supports_model_context(event):
            return
        with self._mcp_injection_lock:
            late_entries = self._mcp_injection_late.pop(platform_session_id, None)
        if not late_entries:
            return
        now = asyncio.get_running_loop().time()
        for late in late_entries.values():
            if now - late.completed.filled_at > _CACHE_TTL_SECONDS:
                continue
            self._record_injection_success(
                late.effect,
                late.row,
                variables,
                staged_variable_updates,
            )
            await self._append_injected_mcp_result(
                server=late.effect.server,
                tool=late.effect.tool,
                raw_result=late.completed.raw_result,
                event=event,
                variables=variables,
                context_parts=context_parts,
            )

    async def _run_cached_mcp_injection(
        self,
        effect: Any,
        row: RuleDefinitionRow,
        arguments: dict[str, Any],
        event: HookEvent,
        platform_session_id: str,
        call_key: str,
    ) -> _CompletedMcpInjection | None:
        try:
            dispatch = self._mcp_dispatcher(effect.server, effect.tool, arguments, event)
            result = await asyncio.wait_for(dispatch, timeout=_BACKGROUND_TIMEOUT_SECONDS)
            if not mcp_call_succeeded(result):
                call_result = result.get("result") if isinstance(result, dict) else None
                error = (
                    call_result.get("error", "unknown")
                    if isinstance(call_result, dict)
                    else str(call_result or "no result")
                )
                logger.warning(
                    "Cached mcp_call %s/%s failed (rule %s): %s",
                    effect.server,
                    effect.tool,
                    row.name,
                    error,
                )
                return None

            raw_result = result.get("result") if isinstance(result, dict) else None
            completed = _CompletedMcpInjection(
                raw_result=raw_result,
                filled_at=asyncio.get_running_loop().time(),
            )
            self._cache_mcp_injection_result(
                platform_session_id,
                call_key,
                completed,
                effect=effect,
                row=row,
            )
            return completed
        except TimeoutError:
            logger.warning(
                "Cached mcp_call %s/%s timed out after %ss (rule %s)",
                effect.server,
                effect.tool,
                _BACKGROUND_TIMEOUT_SECONDS,
                row.name,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Cached mcp_call %s/%s raised (rule %s)",
                effect.server,
                effect.tool,
                row.name,
                exc_info=True,
            )
            return None
        finally:
            with self._mcp_injection_lock:
                self._mcp_injection_inflight.pop(call_key, None)
                self._mcp_injection_abandoned.discard(call_key)

    def _cache_mcp_injection_result(
        self,
        session_id: str,
        call_key: str,
        completed: _CompletedMcpInjection,
        *,
        effect: Any,
        row: RuleDefinitionRow,
    ) -> None:
        with self._mcp_injection_lock:
            _put_session_lru(self._mcp_injection_cache, session_id, call_key, completed)
            # Queued under the same lock as the fill, so a waiter that gives up
            # either sees this result in the cache or leaves it for late delivery.
            if call_key in self._mcp_injection_abandoned:
                self._mcp_injection_abandoned.discard(call_key)
                _put_session_lru(
                    self._mcp_injection_late,
                    session_id,
                    call_key,
                    _LateMcpInjection(effect=effect, row=row, completed=completed),
                )

    async def _append_injected_mcp_result(
        self,
        *,
        server: str,
        tool: str,
        raw_result: Any,
        event: HookEvent,
        variables: dict[str, Any],
        context_parts: list[ContextPart],
    ) -> None:
        if not raw_result:
            return
        formatted: str | None = None
        platform_session_id = event.metadata.get("_platform_session_id")
        if not isinstance(platform_session_id, str) or not platform_session_id:
            platform_session_id = None

        memory_result_handled = False
        if isinstance(raw_result, dict):
            memory_result_handled, formatted, new_lesson_ids = await offload(
                self._format_memory_backed_result,
                server=server,
                tool=tool,
                result=raw_result,
                event=event,
                platform_session_id=platform_session_id,
                variables=variables,
            )
            if new_lesson_ids and platform_session_id:
                stage_append_set_variables(
                    platform_session_id,
                    "injected_review_lesson_ids",
                    new_lesson_ids,
                )
        if not memory_result_handled and (server, tool) != (
            "gobby-agents",
            "cancel_stale_helpers",
        ):
            if not _is_empty_inject_payload(raw_result):
                from gobby.hooks.dispatchers.mcp import format_discovery_result

                formatted = format_discovery_result({"tool": tool, "result": raw_result})
        if formatted:
            context_parts.append((f"mcp:{server}/{tool}", formatted))
