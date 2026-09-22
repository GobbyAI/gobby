"""Non-blocking delivery for optional MCP-backed rule context."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from gobby.workflows.engine.evaluation import EvaluationContext

logger = logging.getLogger(__name__)

_NON_BLOCKING_INJECTION_TOOLS = frozenset(
    {
        ("gobby-memory", "surface_memories"),
        ("gobby-review-learning", "recall_review_lessons_by_class"),
        ("gobby-review-learning", "recall_review_lessons_for_files"),
        ("gobby-skills", "list_hubs"),
    }
)
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_PENDING_SESSIONS = 512
_MAX_PENDING_PER_SESSION = 16


@dataclass(frozen=True, slots=True)
class _PendingMcpInjection:
    server: str
    tool: str
    result: Any
    success_variable: str | None
    delivery: str | None
    allow_reserved_variable: bool


class NonBlockingMcpInjectionMixin(DeliveryFormattingMixin):
    """Run optional context lookups outside the hook's completion path."""

    db: Any
    _mcp_dispatcher: Any
    _mcp_injection_lock: threading.Lock
    _mcp_injection_inflight: set[str]
    _pending_mcp_injections: OrderedDict[str, deque[_PendingMcpInjection]]

    def _initialize_nonblocking_mcp_injections(self) -> None:
        self._mcp_injection_lock = threading.Lock()
        self._mcp_injection_inflight = set()
        self._pending_mcp_injections = OrderedDict()

    @staticmethod
    def _is_nonblocking_mcp_injection(effect: Any) -> bool:
        return bool(
            effect.inject_result
            and not effect.block_on_failure
            and not effect.block_on_success
            and (effect.server, effect.tool) in _NON_BLOCKING_INJECTION_TOOLS
        )

    def _schedule_nonblocking_mcp_injection(
        self,
        effect: Any,
        row: RuleDefinitionRow,
        arguments: dict[str, Any],
        event: HookEvent,
    ) -> bool:
        """Schedule a bounded lookup, returning False when later delivery is impossible."""
        platform_session_id = event.metadata.get("_platform_session_id")
        if not isinstance(platform_session_id, str) or not platform_session_id:
            return False

        call_key = json.dumps(
            [platform_session_id, effect.server, effect.tool, arguments],
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        with self._mcp_injection_lock:
            if call_key in self._mcp_injection_inflight:
                return True
            self._mcp_injection_inflight.add(call_key)

        create_background_task(
            self._run_nonblocking_mcp_injection(
                effect,
                row,
                dict(arguments),
                event,
                platform_session_id,
                call_key,
            )
        )
        return True

    async def _run_nonblocking_mcp_injection(
        self,
        effect: Any,
        row: RuleDefinitionRow,
        arguments: dict[str, Any],
        event: HookEvent,
        platform_session_id: str,
        call_key: str,
    ) -> None:
        timeout_seconds = effect.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS
        try:
            dispatch = self._mcp_dispatcher(effect.server, effect.tool, arguments, event)
            result = await asyncio.wait_for(dispatch, timeout=timeout_seconds)
            if not mcp_call_succeeded(result):
                call_result = result.get("result") if isinstance(result, dict) else None
                error = (
                    call_result.get("error", "unknown")
                    if isinstance(call_result, dict)
                    else str(call_result or "no result")
                )
                logger.warning(
                    "Non-blocking mcp_call %s/%s failed (rule %s): %s",
                    effect.server,
                    effect.tool,
                    row.name,
                    error,
                )
                return
            raw_result = result.get("result") if isinstance(result, dict) else None
            self._enqueue_pending_mcp_injection(
                platform_session_id,
                _PendingMcpInjection(
                    server=effect.server,
                    tool=effect.tool,
                    result=raw_result,
                    success_variable=effect.success_variable,
                    delivery=effect.delivery,
                    allow_reserved_variable=is_internal_rule(row),
                ),
            )
        except TimeoutError:
            logger.warning(
                "Non-blocking mcp_call %s/%s timed out after %ss (rule %s)",
                effect.server,
                effect.tool,
                timeout_seconds,
                row.name,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Non-blocking mcp_call %s/%s raised (rule %s)",
                effect.server,
                effect.tool,
                row.name,
                exc_info=True,
            )
        finally:
            with self._mcp_injection_lock:
                self._mcp_injection_inflight.discard(call_key)

    def _enqueue_pending_mcp_injection(
        self,
        session_id: str,
        injection: _PendingMcpInjection,
    ) -> None:
        with self._mcp_injection_lock:
            queue = self._pending_mcp_injections.pop(
                session_id,
                deque(maxlen=_MAX_PENDING_PER_SESSION),
            )
            if len(queue) == queue.maxlen:
                logger.warning(
                    "Dropping oldest pending MCP injection for saturated session %s",
                    session_id,
                )
            queue.append(injection)
            self._pending_mcp_injections[session_id] = queue
            if len(self._pending_mcp_injections) > _MAX_PENDING_SESSIONS:
                evicted_session_id, _ = self._pending_mcp_injections.popitem(last=False)
                logger.warning(
                    "Dropping pending MCP injections for evicted session %s",
                    evicted_session_id,
                )

    def _take_pending_mcp_injections(self, session_id: str) -> list[_PendingMcpInjection]:
        with self._mcp_injection_lock:
            pending = self._pending_mcp_injections.pop(session_id, ())
        return list(pending)

    async def _drain_pending_mcp_injections(self, evaluation: EvaluationContext) -> None:
        for injection in self._take_pending_mcp_injections(evaluation.session_id):
            if injection.success_variable and (
                injection.allow_reserved_variable
                or not is_reserved_workflow_variable(injection.success_variable)
            ):
                evaluation.variables[injection.success_variable] = True
                if injection.delivery == "on_receipt":
                    evaluation.staged_variable_updates[injection.success_variable] = True
            await self._append_injected_mcp_result(
                server=injection.server,
                tool=injection.tool,
                raw_result=injection.result,
                event=evaluation.event,
                variables=evaluation.variables,
                context_parts=evaluation.context_parts,
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
