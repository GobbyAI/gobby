"""Effect handling for the rule engine.

Handles applying rule effects: set_variable, inject_context, observe,
mcp_call, rewrite_input, load_skill, and block matching.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent
from gobby.hooks.normalization import is_shell_tool
from gobby.memory.recall_constants import MEMORY_RECALL_PRODUCER
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.reserved_variables import is_internal_rule, is_reserved_workflow_variable
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

logger = logging.getLogger(__name__)

REVIEW_LESSON_TAG = "review-lesson"


def _is_empty_inject_payload(result: Any) -> bool:
    """Decide whether an mcp_call result represents nothing worth injecting."""
    if not isinstance(result, dict):
        return result is None or not result
    if result.get("count") == 0:
        return True
    bookkeeping = {"success", "count", "response_time_ms", "recall_request_id", "project_id"}
    content_keys = {key for key in result if key not in bookkeeping}
    if content_keys == {"messages"} and not result.get("messages"):
        return True
    if content_keys == {"memories"} and not result.get("memories"):
        return True
    if content_keys <= {"lessons", "message"} and not result.get("lessons"):
        return True
    return False


def _is_review_lesson_memory(memory: Any) -> bool:
    if not isinstance(memory, dict):
        return False
    tags = memory.get("tags")
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return False
    return REVIEW_LESSON_TAG in tags


class EffectsMixin:
    """Mixin providing effect handling methods for RuleEngine."""

    db: Any
    _skill_manager: Any
    _mcp_dispatcher: Any
    # Durable injection-outcome writer (contract §5); None when recall_signal_hub is off.
    _injection_outcome_recorder: Callable[[list[dict[str, Any]]], None] | None

    if TYPE_CHECKING:
        # Provided by TemplatingMixin at runtime via RuleEngine MRO
        def _render_template(
            self,
            template: str,
            ctx: dict[str, Any],
            allowed_funcs: dict[str, Callable[..., Any]],
        ) -> str: ...

        def _build_allowed_funcs(self, ctx: dict[str, Any]) -> dict[str, Callable[..., Any]]: ...

    def _render_nested_value(
        self,
        value: Any,
        ctx: dict[str, Any],
        allowed_funcs: dict[str, Callable[..., Any]],
    ) -> Any:
        """Recursively render strings inside dict/list payloads."""
        if isinstance(value, str):
            return self._render_template(value, ctx, allowed_funcs)
        if isinstance(value, dict):
            return {
                key: self._render_nested_value(item, ctx, allowed_funcs)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._render_nested_value(item, ctx, allowed_funcs) for item in value]
        return value

    async def _apply_effect(
        self,
        effect: Any,
        row: WorkflowDefinitionRow,
        variables: dict[str, Any],
        ctx: dict[str, Any],
        allowed_funcs: dict[str, Callable[..., Any]],
        context_parts: list[str],
        mcp_calls: list[dict[str, Any]],
    ) -> str | None:
        """Apply a single non-block effect.

        Returns:
            A block reason when an inline mcp_call matches its configured
            blocking outcome, otherwise None.
        """
        if effect.type == "set_variable":
            await asyncio.to_thread(
                self._apply_set_variable,
                effect,
                variables,
                ctx,
                allow_reserved=is_internal_rule(row),
            )

        elif effect.type == "inject_context":
            # NOTE: inject_context templates render with rule evaluation context:
            # event, variables (flattened to top-level), and helper functions.
            # Session data (summary_markdown, task_context) is populated as session
            # variables by the SESSION_START handler before rules evaluate, making
            # them available as {{ session_summary }}, {{ task_context }} in templates.
            if effect.template:
                template_text = await asyncio.to_thread(
                    self._render_template,
                    effect.template,
                    ctx,
                    allowed_funcs,
                )
                # Injected-context fencing lives in the handoff/compact templates
                # themselves (session_start only), not here, so per-turn injections
                # (brevity, memory, task context) stay un-tagged. See
                # context-handoff/inject-previous-session-summary.yaml.
                context_parts.append(template_text)

        elif effect.type == "observe":
            obs_list = variables.get("_observations", [])
            msg = effect.message or ""
            msg = await asyncio.to_thread(self._render_template, msg, ctx, allowed_funcs)
            obs_list.append(
                {
                    "category": effect.category or "general",
                    "message": msg,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "rule": row.name,
                }
            )
            variables["_observations"] = obs_list

        elif effect.type == "mcp_call":
            raw_args = effect.arguments or {}
            rendered_args = await asyncio.to_thread(
                lambda: {
                    k: self._render_template(v, ctx, allowed_funcs) if isinstance(v, str) else v
                    for k, v in raw_args.items()
                }
            )

            # Inline dispatch for inject_result calls — ensures atomicity with
            # sibling effects (e.g. set_variable that tracks injection state).
            # Background calls are always deferred regardless of inject_result.
            if effect.inject_result and not effect.background and self._mcp_dispatcher:
                event = ctx.get("event")
                try:  # Broad catch intentional — external MCP dispatcher is an opaque async callable
                    dr = await self._mcp_dispatcher(
                        effect.server, effect.tool, rendered_args, event
                    )
                    success = isinstance(dr, dict) and dr.get("success", False)
                    if success and effect.success_variable:
                        if is_internal_rule(row) or not is_reserved_workflow_variable(
                            effect.success_variable
                        ):
                            variables[effect.success_variable] = True
                    if success and dr.get("result"):
                        raw_result = dr["result"]
                        formatted: str | None = None
                        event_obj = ctx.get("event")
                        platform_session_id = (
                            event_obj.metadata.get("_platform_session_id")
                            if isinstance(event_obj, HookEvent) and event_obj.metadata
                            else None
                        )

                        if (
                            effect.server,
                            effect.tool,
                        ) == ("gobby-agents", "deliver_pending_messages") and isinstance(
                            raw_result, dict
                        ):
                            formatted = await asyncio.to_thread(
                                self._format_delivery_result,
                                raw_result,
                                platform_session_id,
                                variables,
                            )
                        elif (
                            effect.server,
                            effect.tool,
                        ) == ("gobby-memory", "search_memories") and isinstance(raw_result, dict):
                            formatted = await asyncio.to_thread(
                                self._format_search_memories_result,
                                raw_result,
                                platform_session_id,
                                variables,
                            )
                        elif (effect.server, effect.tool) == (
                            "gobby-review-learning",
                            "recall_review_lessons_for_files",
                        ) and isinstance(raw_result, dict):
                            formatted = await asyncio.to_thread(
                                self._format_review_lessons_result,
                                raw_result,
                                platform_session_id,
                                variables,
                            )
                        elif (effect.server, effect.tool) == (
                            "gobby-agents",
                            "cancel_stale_helpers",
                        ):
                            formatted = None
                        elif not _is_empty_inject_payload(raw_result):
                            from gobby.hooks.dispatchers.mcp import format_discovery_result

                            formatted = format_discovery_result(
                                {"tool": effect.tool, "result": raw_result}
                            )
                        if formatted:
                            context_parts.append(formatted)
                    if effect.block_on_success and success:
                        return f"Intercepted by {effect.server}/{effect.tool} — see context below."
                    if not success:
                        call_result = dr.get("result") if isinstance(dr, dict) else None
                        error = (
                            call_result.get("error", "unknown")
                            if isinstance(call_result, dict)
                            else str(call_result or "no result")
                        )
                        logger.warning(
                            f"Inline mcp_call {effect.server}/{effect.tool} failed "
                            f"(rule {row.name}): {error}",
                        )
                        if effect.block_on_failure:
                            return (
                                f"Auto-heal prerequisite failed: "
                                f"{effect.server}/{effect.tool}: {error}"
                            )
                except Exception as exc:
                    logger.warning(
                        f"Inline mcp_call {effect.server}/{effect.tool} raised (rule {row.name})",
                        exc_info=True,
                    )
                    if effect.block_on_failure:
                        return (
                            f"Auto-heal prerequisite failed: {effect.server}/{effect.tool}: {exc}"
                        )
                return None

            # Deferred dispatch (background, non-inject, or no dispatcher)
            mcp_calls.append(
                {
                    "server": effect.server,
                    "tool": effect.tool,
                    "arguments": rendered_args,
                    "background": effect.background,
                    "inject_result": effect.inject_result,
                    "block_on_failure": effect.block_on_failure,
                    "block_on_success": effect.block_on_success,
                }
            )

        elif effect.type == "rewrite_input":
            if effect.input_updates:
                rendered_updates = await asyncio.to_thread(
                    self._render_nested_value,
                    effect.input_updates,
                    ctx,
                    allowed_funcs,
                )
                # For MCP call_tool, nest updates inside arguments
                # (mirrors the unwrapping in _build_eval_context)
                event = ctx.get("event")
                if event and event.data.get("tool_name") in (
                    "call_tool",
                    "mcp__gobby__call_tool",
                ):
                    original_args = event.data.get("tool_input", {}).get("arguments", {})
                    if isinstance(original_args, str):
                        try:
                            original_args = json.loads(original_args)
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.warning(
                                f"Malformed original_args JSON, defaulting to empty dict: {e}"
                            )
                            original_args = {}
                    if not isinstance(original_args, dict):
                        logger.warning(
                            f"original_args is {type(original_args).__name__}, not dict — defaulting to empty dict"
                        )
                        original_args = {}
                    rendered_updates = {"arguments": {**original_args, **rendered_updates}}
                rewrite_meta = variables.setdefault("_rewrite_input", {})
                rewrite_meta["input_updates"] = rendered_updates
                rewrite_meta["auto_approve"] = effect.auto_approve

        elif effect.type == "set_permission_response":
            permission_meta = variables.setdefault("_permission_response", {})
            if effect.permission_decision:
                permission_meta["permission_decision"] = effect.permission_decision
            if effect.input_updates is not None:
                permission_meta["input_updates"] = await asyncio.to_thread(
                    self._render_nested_value,
                    effect.input_updates,
                    ctx,
                    allowed_funcs,
                )
            if effect.updated_permissions is not None:
                permission_meta["updated_permissions"] = self._render_nested_value(
                    effect.updated_permissions,
                    ctx,
                    allowed_funcs,
                )

        elif effect.type == "set_retry":
            variables["_retry"] = (
                effect.retry if effect.retry is not None else True
            )  # None means default to True; explicit False must stay False.

        elif effect.type == "set_watch_paths":
            if effect.watch_paths is not None:
                variables["_watch_paths"] = self._render_nested_value(
                    effect.watch_paths,
                    ctx,
                    allowed_funcs,
                )

        elif effect.type == "set_worktree_path":
            if effect.worktree_path is not None:
                if (
                    isinstance(effect.worktree_path, str)
                    and "{{" not in effect.worktree_path
                    and "{%" not in effect.worktree_path
                ):
                    variables["_worktree_path"] = effect.worktree_path
                else:
                    variables["_worktree_path"] = await asyncio.to_thread(
                        self._render_nested_value,
                        effect.worktree_path,
                        ctx,
                        allowed_funcs,
                    )

        elif effect.type == "set_elicitation":
            elicitation_meta = variables.setdefault("_elicitation", {})
            if effect.elicitation_action:
                elicitation_meta["action"] = effect.elicitation_action
            if effect.elicitation_content is not None:
                elicitation_meta["content"] = await asyncio.to_thread(
                    self._render_nested_value,
                    effect.elicitation_content,
                    ctx,
                    allowed_funcs,
                )
            if effect.elicitation_error is not None:
                elicitation_meta["error"] = await asyncio.to_thread(
                    self._render_nested_value,
                    effect.elicitation_error,
                    ctx,
                    allowed_funcs,
                )

        elif effect.type == "load_skill":
            if effect.skill:
                from gobby.skills.formatting import skill_fetch_directive

                context_parts.append(skill_fetch_directive(effect.skill))

        return None

    def _format_delivery_result(
        self,
        result: dict[str, Any],
        _platform_session_id: str | None,
        _variables: dict[str, Any],
    ) -> str | None:
        """Inline delivery-time pipeline for deliver_pending_messages results."""
        from gobby.hooks.dispatchers.mcp import format_discovery_result

        if _is_empty_inject_payload(result):
            return None

        messages = result.get("messages") or []
        other_messages: list[Any] = []
        memory_parts: list[str] = []

        for msg in messages:
            if not isinstance(msg, dict):
                other_messages.append(msg)
                continue

            is_memory_message = msg.get("message_type") == "memory_recall"
            content = msg.get("content")
            parsed: Any = None
            if isinstance(content, dict):
                parsed = content
            elif isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    parsed = None

            metadata = msg.get("metadata")
            if (
                not (isinstance(parsed, dict) and parsed.get("type") == "memory_recall")
                and isinstance(metadata, dict)
                and metadata.get("type") == "memory_recall"
            ):
                parsed = metadata

            if isinstance(parsed, dict) and parsed.get("type") == "memory_recall":
                formatted_memory = self._format_memory_recall_delivery(
                    parsed,
                    _platform_session_id,
                    _variables,
                )
                if formatted_memory:
                    memory_parts.append(formatted_memory)
            elif is_memory_message:
                logger.debug("Dropping malformed memory_recall delivery payload")
            else:
                other_messages.append(msg)

        parts: list[str] = list(memory_parts)
        if other_messages:
            message_formatted = format_discovery_result(
                {
                    "tool": "deliver_pending_messages",
                    "result": {"messages": other_messages, "count": len(other_messages)},
                },
            )
            if message_formatted:
                parts.append(message_formatted)

        return "\n\n".join(parts) if parts else None

    def _format_memory_recall_delivery(
        self,
        payload: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
    ) -> str | None:
        """Validate and format a deferred daemon memory recall payload.

        Drops and deliveries log at INFO with the recall_request_id so the
        delivery half of the recall funnel is quantifiable from daemon logs
        and joinable to recall signal events (#17772).
        """
        recall_request_id = payload.get("recall_request_id")
        if payload.get("producer") != MEMORY_RECALL_PRODUCER:
            logger.debug("Dropping memory_recall delivery with non-daemon producer")
            return None
        if payload.get("enabled") is False or payload.get("disabled") is True:
            logger.debug("Dropping disabled memory_recall delivery payload")
            return None

        origin_turn_seq = payload.get("origin_turn_seq")
        parent_turn_seq = variables.get("parent_turn_seq")
        valid_origin_seq = isinstance(origin_turn_seq, int) and not isinstance(
            origin_turn_seq, bool
        )
        recall_context = {
            "recall_request_id": recall_request_id,
            "caller": "memory.recall",
            "project_id": payload.get("project_id"),
            "turn_seq": origin_turn_seq if valid_origin_seq else None,
        }
        if (
            not valid_origin_seq
            or not isinstance(parent_turn_seq, int)
            or isinstance(parent_turn_seq, bool)
        ):
            logger.info(
                "Dropping memory_recall delivery without valid turn sequence: "
                "recall_request_id=%s origin=%s parent=%s",
                recall_request_id,
                origin_turn_seq,
                parent_turn_seq,
            )
            self._record_payload_drop(
                payload, platform_session_id, recall_context, "invalid_turn_seq"
            )
            return None
        if origin_turn_seq != parent_turn_seq - 1:
            logger.info(
                "Dropping stale memory_recall delivery: recall_request_id=%s "
                "origin=%s parent=%s reason=delivery_turn_seq_mismatch",
                recall_request_id,
                origin_turn_seq,
                parent_turn_seq,
            )
            self._record_payload_drop(
                payload, platform_session_id, recall_context, "stale_delivery"
            )
            return None

        memories = payload.get("memories")
        if not isinstance(memories, list):
            logger.info(
                "Dropping memory_recall delivery with malformed memories: recall_request_id=%s",
                recall_request_id,
            )
            return None
        formatted = self._format_search_memories_result(
            {"memories": memories},
            platform_session_id,
            variables,
            recall_context=recall_context,
        )
        if formatted is None:
            logger.info(
                "Dropping memory_recall delivery emptied by review-lesson filter or "
                "delivery dedup: recall_request_id=%s memories=%d reason=delivery_dedup",
                recall_request_id,
                len(memories),
            )
            return None
        logger.info(
            "Delivered memory_recall injection: recall_request_id=%s payload_memories=%d "
            "origin_turn_seq=%s",
            recall_request_id,
            len(memories),
            origin_turn_seq,
        )
        return formatted

    def _format_search_memories_result(
        self,
        result: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
        *,
        recall_context: dict[str, Any] | None = None,
    ) -> str | None:
        """Inline pipeline for search_memories results.

        When the outcome recorder is wired and the payload is joinable
        (recall_request_id + platform session), every memory's final
        injected-vs-filtered decision is persisted (contract §5).
        """
        del variables
        from gobby.hooks.dispatchers.mcp import format_project_memories_with_outcome

        if _is_empty_inject_payload(result):
            return None

        memories = result.get("memories") or []
        recall_ctx = recall_context or {
            "recall_request_id": result.get("recall_request_id"),
            "caller": "mcp_proxy.memory.search_memories",
            "project_id": result.get("project_id"),
            "turn_seq": None,
        }
        rows: list[dict[str, Any]] = []
        group_by_id: dict[str, str | None] = {}
        kept: list[Any] = []
        for memory in memories:
            if isinstance(memory, dict):
                memory_id = memory.get("id")
                if isinstance(memory_id, str) and memory_id:
                    memory_type = memory.get("type")
                    group_by_id[memory_id] = memory_type if isinstance(memory_type, str) else None
            if _is_review_lesson_memory(memory):
                self._append_outcome_row(
                    rows,
                    memory,
                    platform_session_id,
                    recall_ctx,
                    outcome="filtered",
                    drop_reason="review_lesson",
                )
                continue
            kept.append(memory)
        if not kept:
            self._record_injection_outcomes(rows)
            return None

        new_memories, dedup_dropped = self._filter_new_memories(kept, platform_session_id)
        for memory in dedup_dropped:
            self._append_outcome_row(
                rows,
                memory,
                platform_session_id,
                recall_ctx,
                outcome="filtered",
                drop_reason="already_injected",
            )
        if not new_memories:
            self._record_injection_outcomes(rows)
            return None

        text, render_outcome = format_project_memories_with_outcome(new_memories)
        for memory_id in render_outcome.empty_content_ids:
            self._append_outcome_row(
                rows,
                memory_id,
                platform_session_id,
                recall_ctx,
                outcome="filtered",
                drop_reason="empty_content",
            )
        for memory_id in render_outcome.omitted_ids:
            self._append_outcome_row(
                rows,
                memory_id,
                platform_session_id,
                recall_ctx,
                outcome="filtered",
                drop_reason="budget",
            )
        for position, memory_id in enumerate(render_outcome.rendered_ids):
            self._append_outcome_row(
                rows,
                memory_id,
                platform_session_id,
                recall_ctx,
                outcome="injected",
                injection_position=position,
                injection_group=group_by_id.get(memory_id),
            )
        self._track_injected_ids(render_outcome.rendered_ids, platform_session_id)
        self._record_injection_outcomes(rows)
        if rows and render_outcome.rendered_ids:
            self._queue_memory_usefulness(
                platform_session_id, recall_ctx, render_outcome.rendered_ids
            )
        return text or None

    def _queue_memory_usefulness(
        self,
        platform_session_id: str | None,
        recall_context: dict[str, Any],
        rendered_ids: list[str],
    ) -> None:
        """Queue this turn's injected memories for the digest usefulness judge.

        Only reached when injection outcomes were recorded (recall_signal_hub
        on and the payload joinable), so queued entries always have their §2
        join key. The digest pass consumes and clears the queue (#17195).
        """
        if not platform_session_id:
            return
        from gobby.memory.usefulness import PENDING_USEFULNESS_VARIABLE
        from gobby.workflows.state_manager import SessionVariableManager

        try:
            sv_mgr = SessionVariableManager(self.db)
            pending = sv_mgr.get_variables(platform_session_id).get(PENDING_USEFULNESS_VARIABLE)
            pending = list(pending) if isinstance(pending, list) else []
            pending.append(
                {
                    "recall_request_id": recall_context.get("recall_request_id"),
                    "memory_ids": list(rendered_ids),
                    "project_id": recall_context.get("project_id"),
                    "caller": recall_context.get("caller"),
                }
            )
            sv_mgr.set_variable(platform_session_id, PENDING_USEFULNESS_VARIABLE, pending[-8:])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to queue memory-usefulness judgment: %s", exc)

    def _format_review_lessons_result(
        self,
        result: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
    ) -> str | None:
        """Inline pipeline for file-scoped review lesson results."""
        del variables
        from gobby.hooks.dispatchers.mcp import format_discovery_result

        if _is_empty_inject_payload(result):
            return None

        lessons = result.get("lessons") or []
        if not lessons:
            return None
        new_lessons = self._filter_and_track_new_review_lessons(lessons, platform_session_id)
        if not new_lessons:
            return None
        return format_discovery_result(
            {
                "tool": "recall_review_lessons_for_files",
                "result": {"lessons": new_lessons, "count": len(new_lessons)},
            }
        )

    def _filter_new_memories(
        self,
        memories: list[Any],
        platform_session_id: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split memories into not-yet-injected and dedup-dropped lists.

        Read-only against the ``injected_memory_ids`` session variable;
        rendered ids are appended separately via ``_track_injected_ids`` so
        only memories that actually reach the rendered block count as injected.
        """
        from gobby.workflows.state_manager import SessionVariableManager

        new_memories: list[dict[str, Any]] = []
        dedup_dropped: list[dict[str, Any]] = []
        if not memories:
            return new_memories, dedup_dropped

        already: set[str] = set()
        if platform_session_id:
            try:
                sv_mgr = SessionVariableManager(self.db)
                existing_vars = sv_mgr.get_variables(platform_session_id)
                already = set(existing_vars.get("injected_memory_ids", []) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to read injected_memory_ids for dedup: %s", exc)

        seen: set[str] = set()
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            memory_id = memory.get("id")
            if not isinstance(memory_id, str) or not memory_id:
                continue
            if memory_id in seen:
                continue
            seen.add(memory_id)
            if memory_id in already:
                dedup_dropped.append(memory)
                continue
            new_memories.append(memory)

        return new_memories, dedup_dropped

    def _track_injected_ids(
        self,
        memory_ids: list[str],
        platform_session_id: str | None,
    ) -> None:
        """Append rendered memory ids to the ``injected_memory_ids`` session variable."""
        if not memory_ids or not platform_session_id:
            return
        from gobby.workflows.state_manager import SessionVariableManager

        try:
            sv_mgr = SessionVariableManager(self.db)
            sv_mgr.append_to_set_variable(platform_session_id, "injected_memory_ids", memory_ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to append injected_memory_ids: %s", exc)

    def _append_outcome_row(
        self,
        rows: list[dict[str, Any]],
        memory: Any,
        platform_session_id: str | None,
        recall_context: dict[str, Any],
        *,
        outcome: str,
        drop_reason: str | None = None,
        drop_detail: str | None = None,
        injection_position: int | None = None,
        injection_group: str | None = None,
    ) -> None:
        """Append one contract-§5 outcome row when the payload is joinable."""
        if getattr(self, "_injection_outcome_recorder", None) is None:
            return
        if not platform_session_id or not recall_context.get("recall_request_id"):
            return
        memory_id = memory.get("id") if isinstance(memory, dict) else memory
        if not isinstance(memory_id, str) or not memory_id:
            return
        rows.append(
            {
                "session_id": platform_session_id,
                "recall_request_id": recall_context["recall_request_id"],
                "memory_id": memory_id,
                "project_id": recall_context.get("project_id"),
                "outcome": outcome,
                "drop_reason": drop_reason,
                "drop_detail": drop_detail,
                "injection_position": injection_position,
                "injection_group": injection_group,
                "turn_seq": recall_context.get("turn_seq"),
                "caller": recall_context.get("caller") or "memory.recall",
            }
        )

    def _record_injection_outcomes(self, rows: list[dict[str, Any]]) -> None:
        """Persist collected outcome rows through the fail-open recorder."""
        recorder = getattr(self, "_injection_outcome_recorder", None)
        if recorder is None or not rows:
            return
        try:
            recorder(rows)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to record injection outcomes", exc_info=True)

    def _record_payload_drop(
        self,
        payload: dict[str, Any],
        platform_session_id: str | None,
        recall_context: dict[str, Any],
        drop_detail: str,
    ) -> None:
        """Record a whole-payload delivery drop as filtered rows for each memory."""
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return
        rows: list[dict[str, Any]] = []
        for memory in memories:
            self._append_outcome_row(
                rows,
                memory,
                platform_session_id,
                recall_context,
                outcome="filtered",
                drop_reason="other",
                drop_detail=drop_detail,
            )
        self._record_injection_outcomes(rows)

    def _filter_and_track_new_review_lessons(
        self,
        lessons: list[Any],
        platform_session_id: str | None,
    ) -> list[dict[str, Any]]:
        """Filter already-injected review lesson memory ids."""
        from gobby.workflows.state_manager import SessionVariableManager

        new_lessons: list[dict[str, Any]] = []
        if not lessons:
            return new_lessons

        sv_mgr = SessionVariableManager(self.db) if platform_session_id else None
        already: set[str] = set()
        if sv_mgr is not None and platform_session_id:
            try:
                existing_vars = sv_mgr.get_variables(platform_session_id)
                already = set(existing_vars.get("injected_review_lesson_ids", []) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to read injected_review_lesson_ids for dedup: %s", exc)

        seen: set[str] = set()
        for lesson in lessons:
            if not isinstance(lesson, dict):
                continue
            memory_id = lesson.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id:
                continue
            if memory_id in seen or memory_id in already:
                continue
            seen.add(memory_id)
            new_lessons.append(lesson)

        new_ids = [lesson["memory_id"] for lesson in new_lessons if lesson.get("memory_id")]
        if new_ids and sv_mgr is not None and platform_session_id:
            try:
                sv_mgr.append_to_set_variable(
                    platform_session_id,
                    "injected_review_lesson_ids",
                    new_ids,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to append injected_review_lesson_ids: %s", exc)

        return new_lessons

    def _effect_matches_event(self, effect: Any, event: HookEvent) -> bool:
        """Check whether an effect's tool and command selectors match this event."""
        tool_name = event.data.get("tool_name")
        mcp_tool = event.data.get("mcp_tool")
        mcp_server = event.data.get("mcp_server") or event.data.get("server_name")
        command = event.data.get("command")
        if not command:
            tool_input = event.data.get("tool_input")
            if isinstance(tool_input, dict):
                command = tool_input.get("command")

        # If no tools/mcp_tools filter specified, block applies to everything
        has_tool_filter = effect.tools or effect.mcp_tools

        if not has_tool_filter:
            # Check command patterns even without tool filter
            if effect.command_pattern and command:
                if not re.search(effect.command_pattern, command):
                    return False
                if effect.command_not_pattern and re.search(effect.command_not_pattern, command):
                    return False
                return True
            return True

        # Check native tool match
        if effect.tools and tool_name:
            matches_tool = tool_name in effect.tools or (
                is_shell_tool(tool_name) and any(is_shell_tool(name) for name in effect.tools)
            )
            if matches_tool:
                if is_shell_tool(tool_name) and effect.command_pattern and command:
                    if not re.search(effect.command_pattern, command):
                        return False
                    if effect.command_not_pattern and re.search(
                        effect.command_not_pattern, command
                    ):
                        return False
                return True

        # Check MCP tool match
        if effect.mcp_tools and mcp_tool:
            mcp_key = f"{mcp_server}:{mcp_tool}" if mcp_server else mcp_tool
            for pattern in effect.mcp_tools:
                if pattern == mcp_key:
                    return True
                # Support wildcard: "server:*"
                if pattern.endswith(":*") and mcp_server:
                    server_prefix = pattern[:-2]
                    if server_prefix == mcp_server:
                        return True

        return False

    def _should_block(self, effect: Any, event: HookEvent) -> bool:
        """Check if a block effect matches the current tool/event."""
        return self._effect_matches_event(effect, event)

    def _apply_set_variable(
        self,
        effect: Any,
        variables: dict[str, Any],
        eval_context: dict[str, Any],
        *,
        allow_reserved: bool = False,
    ) -> None:
        """Apply a set_variable effect, handling expressions."""
        if effect.variable is None:
            return
        if is_reserved_workflow_variable(effect.variable) and not allow_reserved:
            logger.warning("Rule effect cannot write runtime-managed variable %r", effect.variable)
            return
        value = effect.value

        # Render Jinja2 templates first, before expression evaluation
        if isinstance(value, str) and "{{" in value:
            ctx = eval_context
            allowed_funcs = self._build_allowed_funcs(ctx)
            rendered = self._render_template(value, ctx, allowed_funcs)
            variables[effect.variable] = self._coerce_rendered_value(rendered)
            return

        # If value is a string that looks like an expression, evaluate it
        if isinstance(value, str) and self._is_expression(value):
            try:
                evaluator = SafeExpressionEvaluator(
                    context=eval_context,
                    allowed_funcs=self._build_allowed_funcs(eval_context),
                )
                value = evaluator.evaluate_value(value)
            except Exception as e:
                logger.warning(f"Failed to evaluate set_variable expression '{effect.value}': {e}")
                return

        variables[effect.variable] = value

    def _is_expression(self, value: str) -> bool:
        """Heuristic: is this string an expression rather than a literal?"""
        normalized = SafeExpressionEvaluator._normalize_expr(value)
        expression_indicators = (
            "assistant_response_matches_any(",
            "variables.",
            "event.",
            "tool_input.",
            " + ",
            " - ",
            " and ",
            " or ",
            " not ",
            ".get(",
            "len(",
        )
        return any(indicator in normalized for indicator in expression_indicators)

    @staticmethod
    def _coerce_rendered_value(value: str) -> Any:
        """Coerce a rendered template string to int, float, or bool."""
        s = value.strip()
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return value
