"""Evaluation helpers for the rule engine."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookResponse
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.blocked_tool_recovery import (
    CONSECUTIVE_TOOL_BLOCK_RULE,
    block_reason_signature,
    block_source_for_rule,
    clear_blocked_tool_recovery_state,
    ensure_block_reason,
    extract_rule_name,
    format_aggregated_block_reason,
    log_block,
    remember_blocked_tool_recovery_state,
)
from gobby.workflows.engine.event_utils import (
    _block_tool_name,
    _clear_edit_write_state,
    _get_tool_identity,
    _is_write_like_event_data,
)

logger = logging.getLogger(__name__)


@dataclass
class EvaluationContext:
    """Mutable evaluation state shared across rule-engine helper methods."""

    event: HookEvent
    session_id: str
    variables: dict[str, Any]
    eval_context: dict[str, Any] | None
    is_before_tool: bool
    block_tool_name: str
    context_parts: list[str] = field(default_factory=list)
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class BlockGate:
    """Rendered block gate plus optional delivery acknowledgement metadata."""

    rule_name: str
    reason: str
    acknowledge_variable: str | None = None


class EvaluationMixin:
    """Mixin providing the RuleEngine evaluation loop and response assembly."""

    _event_store: Any

    if TYPE_CHECKING:

        def _render_template(
            self,
            template: str,
            ctx: dict[str, Any],
            allowed_funcs: dict[str, Callable[..., Any]],
        ) -> str: ...

        def _build_eval_context(
            self,
            event: HookEvent,
            variables: dict[str, Any],
            extra_context: dict[str, Any] | None = None,
        ) -> dict[str, Any]: ...

        def _build_allowed_funcs(self, ctx: dict[str, Any]) -> dict[str, Callable[..., Any]]: ...

        def _evaluate_condition(
            self,
            condition: str,
            context: dict[str, Any],
            effect_type: str = "block",
            allowed_funcs: dict[str, Callable[..., Any]] | None = None,
        ) -> bool: ...

        def _effect_matches_event(self, effect: Any, event: HookEvent) -> bool: ...

        async def _apply_effect(
            self,
            effect: RuleEffect,
            row: WorkflowDefinitionRow,
            variables: dict[str, Any],
            ctx: dict[str, Any],
            allowed_funcs: dict[str, Callable[..., Any]],
            context_parts: list[str],
            mcp_calls: list[dict[str, Any]],
        ) -> bool: ...

        def _check_catastrophic_failure(
            self,
            event: HookEvent,
            variables: dict[str, Any],
        ) -> None: ...

    def _manage_after_tool_recovery_state(
        self,
        event: HookEvent,
        variables: dict[str, Any],
    ) -> None:
        """Update failed-tool and edit/write recovery state after tool completion."""
        is_failure = event.metadata.get("is_failure", False) or event.data.get("is_error", False)
        if is_failure:
            variables["tool_block_pending"] = True
            self._check_catastrophic_failure(event, variables)
            return

        # Snapshot before clearing: a parallel non-edit success should not clear
        # edit state created by a tool failure in the same turn.
        had_pending_failure = variables.get("tool_block_pending", False)

        variables["tool_block_pending"] = False
        clear_blocked_tool_recovery_state(variables)
        variables["consecutive_tool_blocks"] = 0

        if variables.get("edit_write_pending"):
            if _is_write_like_event_data(event.data) or not had_pending_failure:
                _clear_edit_write_state(variables)

    def _finalize_block_response(
        self,
        response: HookResponse,
        evaluation: EvaluationContext,
        span: Any,
        *,
        source: str | None = None,
        rule_name: str | None = None,
        fallback_reason: str | None = None,
        warn_detail: str = "block response omitted reason",
    ) -> HookResponse:
        """Normalize block responses, log them, and attach tracing fields."""
        if response.decision == "block":
            resolved_rule_name = (
                rule_name or extract_rule_name(response.reason) or "rule-engine-block"
            )
            resolved_source = source or block_source_for_rule(resolved_rule_name)
            resolved_fallback = fallback_reason or (
                f"Rule enforced by Gobby: [{resolved_rule_name}]\n"
                "Gobby blocked this event without providing a reason. "
                "This is a bug."
            )
            blocked_tool_name = _get_tool_identity(evaluation.event.data)
            response.reason = ensure_block_reason(
                session_id=evaluation.session_id,
                event_type=evaluation.event.event_type,
                tool_name=blocked_tool_name or "-",
                source=resolved_source,
                rule_name=resolved_rule_name,
                reason=response.reason,
                fallback_reason=resolved_fallback,
                warn_detail=warn_detail,
            )
            log_block(
                session_id=evaluation.session_id,
                event_type=evaluation.event.event_type,
                tool_name=blocked_tool_name,
                source=resolved_source,
                rule_name=resolved_rule_name,
                reason=response.reason,
            )
            if evaluation.is_before_tool and resolved_rule_name != CONSECUTIVE_TOOL_BLOCK_RULE:
                remember_blocked_tool_recovery_state(
                    evaluation.variables,
                    tool_name=blocked_tool_name,
                    rule_name=resolved_rule_name,
                    reason=response.reason,
                )
            # Verbose-once: collapse repeat identical blocks within a turn.
            # Dynamic reasons from the same rule still render in full.
            # Cleared on TURN_START.
            # Stored as list[str] because session variables are JSON-persisted.
            shown = evaluation.variables.get("_block_reasons_shown")
            if not isinstance(shown, list):
                shown = []
                evaluation.variables["_block_reasons_shown"] = shown
            block_signature = block_reason_signature(resolved_rule_name, response.reason)
            if block_signature in shown:
                response.reason = (
                    f"Rule enforced by Gobby: [{resolved_rule_name}] "
                    "(full reason shown earlier this turn — scroll up)."
                )
            else:
                shown.append(block_signature)
        if span.is_recording():
            span.set_attribute("final_decision", response.decision)
            if response.reason:
                span.set_attribute("block_reason", response.reason)
        return response

    def _render_rule_block_reason(
        self,
        evaluation: EvaluationContext,
        row: WorkflowDefinitionRow,
        effect: RuleEffect,
        ctx: dict[str, Any],
        allowed_funcs: dict[str, Callable[..., Any]],
    ) -> str:
        reason = ensure_block_reason(
            session_id=evaluation.session_id,
            event_type=evaluation.event.event_type,
            tool_name=_block_tool_name(evaluation.event),
            source="rule",
            rule_name=row.name,
            reason=effect.reason,
            fallback_reason=(
                "Rule block effect omitted a reason. Update the rule "
                "definition to explain why the event was blocked."
            ),
            warn_detail="rule block effect omitted a reason",
        )
        return self._render_template(reason, ctx, allowed_funcs)

    async def _run_rule_loop(
        self,
        rules: list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]],
        evaluation: EvaluationContext,
        *,
        aggregate_blocks: bool,
    ) -> list[BlockGate]:
        block_gates: list[BlockGate] = []

        for row, body in rules:
            # Pre-filter: skip rule if tools field doesn't match current tool
            if body.tools:
                tool_name = evaluation.event.data.get("tool_name", "")
                if tool_name not in body.tools:
                    continue

            # Build fresh eval context with current variables
            ctx = self._build_eval_context(
                evaluation.event,
                evaluation.variables,
                evaluation.eval_context,
            )

            # Build allowed_funcs once per iteration - shared by condition and templates
            allowed_funcs = self._build_allowed_funcs(ctx)

            # Check rule-level `when` condition
            if body.when:
                # Use first effect type for fail-open/closed heuristic
                first_type = body.resolved_effects[0].type if body.resolved_effects else "block"
                if not self._evaluate_condition(body.when, ctx, first_type, allowed_funcs):
                    continue

            if block_gates:
                for effect in body.resolved_effects:
                    if effect.type != "block" or not self._effect_matches_event(
                        effect, evaluation.event
                    ):
                        continue
                    if effect.when and not self._evaluate_condition(
                        effect.when,
                        ctx,
                        effect.type,
                        allowed_funcs,
                    ):
                        continue
                    block_gates.append(
                        BlockGate(
                            rule_name=row.name,
                            reason=self._render_rule_block_reason(
                                evaluation,
                                row,
                                effect,
                                ctx,
                                allowed_funcs,
                            ),
                            acknowledge_variable=effect.acknowledge_variable,
                        )
                    )
                    break
                continue

            # Process effects: non-block effects first, then block (if any)
            effects = body.resolved_effects
            deferred_block: RuleEffect | None = None
            rule_start = time.perf_counter()
            rule_blocked = False

            for effect in effects:
                if not self._effect_matches_event(effect, evaluation.event):
                    continue

                # Check per-effect `when` condition
                if effect.when:
                    if not self._evaluate_condition(
                        effect.when,
                        ctx,
                        effect.type,
                        allowed_funcs,
                    ):
                        continue

                if effect.type == "block":
                    # Defer block to after all sibling non-block effects
                    deferred_block = effect
                    continue

                # Apply non-block effects immediately
                should_continue = await self._apply_effect(
                    effect,
                    row,
                    evaluation.variables,
                    ctx,
                    allowed_funcs,
                    evaluation.context_parts,
                    evaluation.mcp_calls,
                )
                if not should_continue:
                    break  # Inline dispatch failed - skip remaining effects

            # Now apply deferred block (if any)
            if deferred_block is not None:
                if self._effect_matches_event(deferred_block, evaluation.event):
                    rule_blocked = True
                    rendered_block_reason = self._render_rule_block_reason(
                        evaluation,
                        row,
                        deferred_block,
                        ctx,
                        allowed_funcs,
                    )
                    block_gates.append(
                        BlockGate(
                            rule_name=row.name,
                            reason=rendered_block_reason,
                            acknowledge_variable=deferred_block.acknowledge_variable,
                        )
                    )
                    # Track the blocked tool so repeated retries can escalate,
                    # but do not mark this as a tool execution failure.
                    if evaluation.is_before_tool:
                        evaluation.variables["_last_blocked_tool"] = _get_tool_identity(
                            evaluation.event.data
                        )
                        # Blocked edit/write never executed - nothing to recover
                        if _is_write_like_event_data(evaluation.event.data):
                            _clear_edit_write_state(evaluation.variables)

            # Record rule evaluation metric
            if self._event_store:
                rule_latency = (time.perf_counter() - rule_start) * 1000
                try:
                    self._event_store.record_event(
                        event_type="rule_eval",
                        name=row.name,
                        session_id=evaluation.session_id,
                        success=not rule_blocked,
                        result="block" if rule_blocked else "allow",
                        latency_ms=rule_latency,
                    )
                except Exception as e:
                    logger.debug("Metrics recording failed: %s", e, exc_info=True)

            if rule_blocked:
                # First block runs normal effects; later aggregation is read-only.
                if not aggregate_blocks:
                    break

        return block_gates

    def _assemble_response(
        self,
        evaluation: EvaluationContext,
        *,
        override_decision: str | None,
        override_reason: str | None,
        block_gates: list[BlockGate],
        include_rule_outputs: bool = True,
    ) -> HookResponse:
        block_reason: str | None = None
        if len(block_gates) > 1:
            block_reason = format_aggregated_block_reason(
                [(gate.rule_name, gate.reason) for gate in block_gates],
                tool_name=evaluation.block_tool_name,
            )
        elif block_gates:
            gate = block_gates[0]
            block_reason = f"Rule enforced by Gobby: [{gate.rule_name}]\n{gate.reason}"

        ctx_str = "\n\n".join(evaluation.context_parts) if evaluation.context_parts else None
        meta = {"mcp_calls": evaluation.mcp_calls} if evaluation.mcp_calls else {}

        if not include_rule_outputs:
            if override_decision == "block":
                return HookResponse(
                    decision="block",
                    reason=override_reason or "",
                    context=ctx_str,
                    metadata=meta,
                )
            return HookResponse(decision="allow", context=ctx_str, metadata=meta)

        # Propagate rewrite_input from variables to response
        rewrite_meta = evaluation.variables.pop("_rewrite_input", None)
        modified_input: dict[str, Any] | None = None
        auto_approve = False
        if rewrite_meta and isinstance(rewrite_meta, dict):
            modified_input = rewrite_meta.get("input_updates")
            auto_approve = rewrite_meta.get("auto_approve", False)

        permission_meta = evaluation.variables.pop("_permission_response", None)
        permission_decision: str | None = None
        updated_permissions: list[dict[str, Any]] | None = None
        if permission_meta and isinstance(permission_meta, dict):
            if permission_meta.get("input_updates") is not None:
                modified_input = permission_meta.get("input_updates")
            permission_decision = permission_meta.get("permission_decision")
            updated_permissions = permission_meta.get("updated_permissions")

        watch_paths = evaluation.variables.pop("_watch_paths", None)
        worktree_path = evaluation.variables.pop("_worktree_path", None)
        retry = bool(evaluation.variables.pop("_retry", False))
        elicitation_meta = evaluation.variables.pop("_elicitation", None)
        elicitation_action: str | None = None
        elicitation_content: dict[str, Any] | None = None
        elicitation_error: str | None = None
        if elicitation_meta and isinstance(elicitation_meta, dict):
            elicitation_action = elicitation_meta.get("action")
            elicitation_content = elicitation_meta.get("content")
            elicitation_error = elicitation_meta.get("error")

        response_kwargs: dict[str, Any] = {
            "metadata": meta,
            "modified_input": modified_input,
            "auto_approve": auto_approve,
            "permission_decision": permission_decision,
            "updated_permissions": updated_permissions,
            "retry": retry,
            "watch_paths": watch_paths,
            "worktree_path": worktree_path,
            "elicitation_action": elicitation_action,
            "elicitation_content": elicitation_content,
            "elicitation_error": elicitation_error,
        }

        if override_decision == "block":
            return HookResponse(
                decision="block",
                reason=override_reason or "",
                context=ctx_str,
                **response_kwargs,
            )
        if override_decision == "allow":
            return HookResponse(
                decision="allow",
                context=ctx_str,
                **response_kwargs,
            )
        if block_reason:
            for gate in block_gates:
                if gate.acknowledge_variable:
                    evaluation.variables[gate.acknowledge_variable] = True
            return HookResponse(
                decision="block",
                reason=block_reason,
                context=ctx_str,
                **response_kwargs,
            )
        return HookResponse(
            decision="allow",
            context=ctx_str,
            **response_kwargs,
        )
