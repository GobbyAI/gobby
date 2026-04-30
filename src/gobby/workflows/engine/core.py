"""Core rule engine with single-pass evaluation loop.

Rules are stateless event handlers: event comes in, conditions match, effect fires.
Effect types: block, set_variable, inject_context, mcp_call, observe,
rewrite_input, load_skill.
"""

import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics_events import MetricsEventStore

from opentelemetry.trace import Status, StatusCode

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.telemetry.tracing import create_span
from gobby.workflows.definitions import (
    RuleDefinitionBody,
    RuleEffect,
    RuleTriggerEvent,
)
from gobby.workflows.engine.effects import EffectsMixin
from gobby.workflows.engine.enforcement import EnforcementMixin
from gobby.workflows.engine.templating import TemplatingMixin
from gobby.workflows.state_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)

_RULE_REASON_RE = re.compile(r"^Rule enforced by Gobby: \[([^\]]+)\]")

_TURN_START_EVENT_VALUES = frozenset(
    {
        HookEventType.BEFORE_AGENT.value,
    }
)

_TURN_END_EVENT_VALUES = frozenset(
    {
        HookEventType.AFTER_AGENT.value,
        HookEventType.STOP.value,
        HookEventType.STOP_FAILURE.value,
    }
)


def _get_tool_identity(event_data: dict[str, Any]) -> str:
    """Return effective tool identity for consecutive-block tracking.

    For MCP calls (mcp__gobby__call_tool / call_tool), returns 'server:tool'
    so different MCP tools are tracked independently. This prevents one failing
    MCP tool from blocking all other MCP tools.
    """
    tool_name = event_data.get("tool_name", "")
    if tool_name in ("call_tool", "mcp__gobby__call_tool"):
        tool_input = event_data.get("tool_input") or {}
        if isinstance(tool_input, dict):
            server = tool_input.get("server_name", "")
            tool = tool_input.get("tool_name", "")
            if server and tool:
                return f"{server}:{tool}"
    return str(tool_name)


def _is_pipeline_direct_mcp_event(event: HookEvent) -> bool:
    """Return True for synthetic direct MCP calls emitted by pipeline sessions."""
    if event.source != SessionSource.PIPELINE:
        return False

    tool_name = event.data.get("tool_name", "")
    return tool_name in ("call_tool", "mcp__gobby__call_tool")


def _event_value(event_type: HookEventType | str) -> str:
    if isinstance(event_type, HookEventType):
        return event_type.value
    return str(event_type)


def _is_turn_start_event(event_type: HookEventType | str) -> bool:
    return _event_value(event_type) in _TURN_START_EVENT_VALUES


def _is_turn_end_event(event_type: HookEventType | str) -> bool:
    return _event_value(event_type) in _TURN_END_EVENT_VALUES


def _resolve_rule_events(event_type: HookEventType | str) -> list[RuleTriggerEvent]:
    """Resolve an incoming hook event into rule trigger events."""
    resolved: list[RuleTriggerEvent] = []
    raw_value = _event_value(event_type)

    if _is_turn_start_event(raw_value):
        resolved.append(RuleTriggerEvent.TURN_START)

    if _is_turn_end_event(raw_value):
        resolved.append(RuleTriggerEvent.TURN_END)

    try:
        resolved.append(RuleTriggerEvent(raw_value))
    except ValueError:
        pass

    deduped: list[RuleTriggerEvent] = []
    seen: set[RuleTriggerEvent] = set()
    for trigger in resolved:
        if trigger not in seen:
            deduped.append(trigger)
            seen.add(trigger)
    return deduped


def _clear_edit_write_state(variables: dict[str, Any]) -> None:
    """Clear edit/write pending state and stop-block counter."""
    variables["edit_write_pending"] = False
    variables["edit_write_stop_blocks"] = 0


def _is_write_like_event_data(event_data: dict[str, Any]) -> bool:
    """Return True when normalized event data represents a file mutation."""
    return bool(event_data.get("canonical_repo_mutation")) or (
        event_data.get("canonical_tool_kind") == "write"
    )


def _block_tool_name(event: HookEvent) -> str:
    """Return tool identity used in structured block logs."""
    tool_name = _get_tool_identity(event.data)
    return tool_name or "-"


def _extract_rule_name(reason: str | None) -> str | None:
    """Extract rule name from a standard Gobby block reason prefix."""
    if not reason:
        return None
    match = _RULE_REASON_RE.match(reason)
    if not match:
        return None
    return match.group(1)


def _block_source_for_rule(rule_name: str) -> str:
    """Map block rule names onto observability source labels."""
    if rule_name in {"agent-tool-enforcement", "step-tool-enforcement"}:
        return "step-enforcement"
    return "rule"


def _warn_block_fallback(
    *,
    session_id: str,
    event: HookEvent,
    source: str,
    rule_name: str,
    detail: str,
) -> None:
    """Emit a warning when block handling has to synthesize a reason."""
    logger.warning(
        "BLOCK fallback session=%s event=%s tool=%s source=%s rule=%s detail=%s",
        session_id,
        _event_value(event.event_type),
        _block_tool_name(event),
        source,
        rule_name,
        detail,
    )


def _ensure_block_reason(
    *,
    session_id: str,
    event: HookEvent,
    source: str,
    rule_name: str,
    reason: str | None,
    fallback_reason: str,
    warn_detail: str,
) -> str:
    """Return a non-empty block reason, warning when fallback text is required."""
    cleaned = (reason or "").strip()
    if cleaned:
        return cleaned
    _warn_block_fallback(
        session_id=session_id,
        event=event,
        source=source,
        rule_name=rule_name,
        detail=warn_detail,
    )
    return fallback_reason


def _log_block(
    *,
    session_id: str,
    event: HookEvent,
    source: str,
    rule_name: str,
    reason: str,
) -> None:
    """Emit structured block log for observability and downstream debugging."""
    logger.info(
        "BLOCK session=%s event=%s tool=%s source=%s rule=%s reason=%s",
        session_id,
        _event_value(event.event_type),
        _block_tool_name(event),
        source,
        rule_name,
        reason,
    )


class RuleEngine(EffectsMixin, TemplatingMixin, EnforcementMixin):
    """Single-pass rule evaluation engine.

    Loads rules from workflow_definitions (workflow_type='rule'),
    applies session overrides, evaluates in priority order.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        skill_manager: Any | None = None,
        metrics_event_store: "MetricsEventStore | None" = None,
        mcp_dispatcher: Any | None = None,
        runner: Any | None = None,
        completion_registry: Any | None = None,
        task_manager: Any | None = None,
    ):
        self.db = db
        self.definition_manager = LocalWorkflowDefinitionManager(db)
        self.instance_manager = WorkflowInstanceManager(db)
        self.workflow_audit = WorkflowAuditManager(db)
        self._skill_manager = skill_manager
        self._event_store = metrics_event_store
        self._mcp_dispatcher = mcp_dispatcher
        self._runner = runner
        self._completion_registry = completion_registry
        self._task_manager = task_manager

    async def evaluate(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
    ) -> HookResponse:
        """Evaluate all matching rules for an event.

        Args:
            event: The hook event to evaluate.
            session_id: Current session ID (for overrides).
            variables: Session variables dict (mutated in-place by set_variable).
            eval_context: Additional eval context (LazyBool thunks, etc).

        Returns:
            HookResponse with merged results from all matching rules.
        """
        with create_span(
            "rules.evaluate",
            attributes={"event_type": str(event.event_type), "session_id": session_id},
        ) as span:
            try:

                def finalize_response(
                    response: HookResponse,
                    *,
                    source: str | None = None,
                    rule_name: str | None = None,
                    fallback_reason: str | None = None,
                    warn_detail: str = "block response omitted reason",
                ) -> HookResponse:
                    """Normalize block responses, log them, and attach tracing fields."""
                    if response.decision == "block":
                        resolved_rule_name = (
                            rule_name or _extract_rule_name(response.reason) or "rule-engine-block"
                        )
                        resolved_source = source or _block_source_for_rule(resolved_rule_name)
                        resolved_fallback = fallback_reason or (
                            f"Rule enforced by Gobby: [{resolved_rule_name}]\n"
                            "Gobby blocked this event without providing a reason. "
                            "This is a bug."
                        )
                        response.reason = _ensure_block_reason(
                            session_id=session_id,
                            event=event,
                            source=resolved_source,
                            rule_name=resolved_rule_name,
                            reason=response.reason,
                            fallback_reason=resolved_fallback,
                            warn_detail=warn_detail,
                        )
                        _log_block(
                            session_id=session_id,
                            event=event,
                            source=resolved_source,
                            rule_name=resolved_rule_name,
                            reason=response.reason,
                        )
                        # Verbose-once: collapse repeat blocks of the same rule
                        # within a turn down to a single line. Cleared on TURN_START.
                        # Stored as list[str] because session variables are JSON-persisted.
                        shown = variables.get("_block_reasons_shown")
                        if not isinstance(shown, list):
                            shown = []
                            variables["_block_reasons_shown"] = shown
                        if resolved_rule_name in shown:
                            response.reason = (
                                f"Rule enforced by Gobby: [{resolved_rule_name}] "
                                "(full reason shown earlier this turn — scroll up)."
                            )
                        else:
                            shown.append(resolved_rule_name)
                    if span.is_recording():
                        span.set_attribute("final_decision", response.decision)
                        if response.reason:
                            span.set_attribute("block_reason", response.reason)
                    return response

                if isinstance(event.data, dict):
                    normalize_tool_fields(event.data)

                resolved_rule_events = _resolve_rule_events(event.event_type)
                if not resolved_rule_events:
                    return HookResponse(decision="allow")

                raw_event_value = _event_value(event.event_type)
                is_before_tool = raw_event_value == HookEventType.BEFORE_TOOL.value
                is_after_tool = raw_event_value == HookEventType.AFTER_TOOL.value
                is_turn_start = RuleTriggerEvent.TURN_START in resolved_rule_events
                is_turn_end = RuleTriggerEvent.TURN_END in resolved_rule_events

                # Check global enforcement toggle
                config_store = ConfigStore(self.db)
                if config_store.get("rules.enforcement_enabled") is False:
                    return HookResponse(decision="allow")

                # Collect mcp_call effects from hardcoded rules and DB rules.
                # Initialized early so hardcoded turn-start rules can append.
                mcp_calls: list[dict[str, Any]] = []

                # Auto-track consecutive retries after a blocked BEFORE_TOOL.
                # _last_blocked_tool is only set by pre-execution gate/enforcement blocks.
                # tool_block_pending is reserved for real tool execution failures.
                if is_before_tool and variables.get("_last_blocked_tool"):
                    if _is_pipeline_direct_mcp_event(event):
                        # Synthetic pipeline MCP events clear block state so the next real user tool starts from 0.
                        variables["consecutive_tool_blocks"] = 0
                        variables["_last_blocked_tool"] = ""
                    else:
                        tool_name = _get_tool_identity(event.data)
                        last_blocked = variables.get("_last_blocked_tool", "")
                        if tool_name == last_blocked:
                            count = variables.get("consecutive_tool_blocks", 0) + 1
                            variables["consecutive_tool_blocks"] = count
                            max_attempts = int(
                                variables.get("max_consecutive_blocked_tool_attempts", 5)
                            )
                            total_attempts = count + 1
                            if total_attempts >= max_attempts:
                                resp = HookResponse(
                                    decision="block",
                                    reason=(
                                        "Rule enforced by Gobby: [consecutive-tool-block]\n"
                                        f"You have attempted {tool_name} {total_attempts} times consecutively "
                                        "without addressing the error.\n"
                                        "STOP retrying the same action. Read the previous error messages "
                                        "and take a DIFFERENT action to resolve the underlying issue first."
                                    ),
                                )
                                return finalize_response(
                                    resp,
                                    source="rule",
                                    rule_name="consecutive-tool-block",
                                )
                        else:
                            # Different tool — reset counter, let it through to rule evaluation
                            variables["consecutive_tool_blocks"] = 0
                # Track edit/write attempts — set pending on pre-tool
                if is_before_tool:
                    if _is_write_like_event_data(event.data):
                        variables["edit_write_pending"] = True

                elif is_turn_start:
                    variables["consecutive_tool_blocks"] = 0
                    variables["_last_blocked_tool"] = ""
                    variables["tool_block_pending"] = False
                    variables["stop_attempts"] = 0
                    variables["_block_reasons_shown"] = []

                    # [auto-discover-servers] — hardcoded, always-on
                    # Seed progressive discovery on first prompt so agents
                    # don't need to call list_mcp_servers() manually.
                    if not variables.get("servers_listed"):
                        mcp_calls.append(
                            {
                                "server": "_proxy",
                                "tool": "list_mcp_servers",
                                "arguments": {"name_filter": "gobby-*"},
                                "inject_result": True,
                            }
                        )
                        variables["servers_listed"] = True

                # Auto-increment stop attempts (universal — not configurable)
                if is_turn_end:
                    variables["stop_attempts"] = variables.get("stop_attempts", 0) + 1
                    logger.debug(
                        f"TURN_END gate diagnostics: session_id={session_id}, raw_event={raw_event_value}, auto_task_ref={variables.get('auto_task_ref')!r}, stop_attempts={variables['stop_attempts']}, task_claimed={variables.get('task_claimed')}, claimed_tasks={variables.get('claimed_tasks')}, errors_resolved={variables.get('errors_resolved')}, error_triage_blocks={variables.get('error_triage_blocks', 0)}, edit_write_pending={variables.get('edit_write_pending')}, tool_block_pending={variables.get('tool_block_pending')}",
                    )

                # 1. Load enabled rules for this event, sorted by priority
                rules = self._load_rules(resolved_rule_events)

                # 2. Apply session overrides
                overrides = self._load_session_overrides(session_id)
                rules = self._apply_overrides(rules, overrides)

                # 3. Filter by agent_scope
                agent_type = variables.get("_agent_type")
                rules = self._filter_by_agent_scope(rules, agent_type)

                # 4. Filter by active rules (selector-based)
                rules = self._filter_by_active_rules(rules, variables)

                if span.is_recording():
                    span.set_attribute("rule_count", len(rules))
                    span.set_attribute(
                        "rules.resolved_events",
                        [rule_event.value for rule_event in resolved_rule_events],
                    )

                # 4b. Agent-level tool enforcement (broadest scope, preempts everything)
                if is_before_tool:
                    agent_block = self._check_agent_tool_enforcement(event, session_id, variables)
                    if agent_block is not None:
                        variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                        if _is_write_like_event_data(event.data):
                            _clear_edit_write_state(variables)
                        return finalize_response(
                            agent_block,
                            source="step-enforcement",
                            rule_name="agent-tool-enforcement",
                            fallback_reason=(
                                "Rule enforced by Gobby: [agent-tool-enforcement]\n"
                                "Agent-level tool enforcement blocked this tool for the "
                                "current session."
                            ),
                            warn_detail="agent-level enforcement returned an empty reason",
                        )

                # 4c. Step-level tool enforcement (preempts declarative rules)
                if is_before_tool:
                    step_block = self._check_step_tool_enforcement(event, session_id, variables)
                    if step_block is not None:
                        variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                        # Blocked edit/write never executed — nothing to recover
                        if _is_write_like_event_data(event.data):
                            _clear_edit_write_state(variables)
                        return finalize_response(
                            step_block,
                            source="step-enforcement",
                            rule_name="step-tool-enforcement",
                            fallback_reason=(
                                "Rule enforced by Gobby: [step-tool-enforcement]\n"
                                "Current workflow step blocked this tool for the "
                                "active session."
                            ),
                            warn_detail="step enforcement returned an empty reason",
                        )

                # 4c. Step workflow transition processing (after successful MCP tool calls)
                _step_transition_msg: str | None = None
                if is_after_tool:
                    _step_transition_msg = await self._process_step_after_tool(
                        event, session_id, variables
                    )

                # Deferred overrides — these used to early-return, but that skipped rule
                # evaluation entirely, preventing mcp_call effects (like digest-on-response)
                # from being collected. Now we record the override and let the loop run.
                override_decision: str | None = None
                override_reason: str | None = None

                # Force-allow stop (catastrophic failure bypass — self-clearing)
                if is_turn_end and variables.get("force_allow_stop"):
                    variables["force_allow_stop"] = False
                    if variables.get("task_claimed"):
                        logger.warning(
                            f"force_allow_stop suppressed - task_claimed=True, deferring to require-task-close rule (session {session_id})",
                        )
                    else:
                        override_decision = "allow"

                # Auto-block stop when a tool just failed (self-clearing)
                elif is_turn_end and variables.get("tool_block_pending"):
                    variables["tool_block_pending"] = False
                    override_decision = "block"
                    override_reason = (
                        "Rule enforced by Gobby: [tool-failure-recovery]\n"
                        "A tool just failed. Read the error and recover — do not stop."
                    )

                # Block stop when edit/write is pending (failed or in-flight)
                elif is_turn_end and variables.get("edit_write_pending"):
                    edit_stop_blocks = variables.get("edit_write_stop_blocks", 0)
                    if edit_stop_blocks < 3:  # Circuit breaker
                        variables["edit_write_stop_blocks"] = edit_stop_blocks + 1
                        override_decision = "block"
                        override_reason = (
                            "Rule enforced by Gobby: [edit-write-recovery]\n"
                            "Your last file mutation attempt failed. "
                            "Read the error and retry — do not stop."
                        )
                    else:
                        # Circuit breaker tripped — clear and allow stop
                        _clear_edit_write_state(variables)

                if not rules:
                    # Auto-manage tool_block_pending on after_tool execution results.
                    if is_after_tool:
                        is_failure = event.metadata.get("is_failure", False) or event.data.get(
                            "is_error", False
                        )
                        if is_failure:
                            variables["tool_block_pending"] = True
                            self._check_catastrophic_failure(event, variables)
                        else:
                            # Snapshot before clearing — if a tool just failed,
                            # a parallel non-edit success shouldn't clear edit state.
                            had_pending_failure = variables.get("tool_block_pending", False)

                            # Clear tool_block_pending on successful tool completion
                            variables["tool_block_pending"] = False
                            variables["_last_blocked_tool"] = ""
                            variables["consecutive_tool_blocks"] = 0

                            # Clear edit_write_pending when the successful tool is an
                            # edit/write, OR when no failure is pending (stale flag).
                            # Don't clear on non-edit success during a parallel failure
                            # — the edit wasn't recovered yet.
                            if variables.get("edit_write_pending"):
                                if _is_write_like_event_data(event.data) or not had_pending_failure:
                                    _clear_edit_write_state(variables)
                    # Honour hardcoded override decisions (e.g. tool_block_pending stop gate)
                    # even when no declarative rules are installed for this event.
                    _no_rules_ctx = _step_transition_msg or None
                    meta = {"mcp_calls": mcp_calls} if mcp_calls else {}
                    if override_decision == "block":
                        resp = HookResponse(
                            decision="block",
                            reason=override_reason or "",
                            context=_no_rules_ctx,
                            metadata=meta,
                        )
                    elif override_decision == "allow":
                        resp = HookResponse(decision="allow", context=_no_rules_ctx, metadata=meta)
                    else:
                        resp = HookResponse(decision="allow", context=_no_rules_ctx, metadata=meta)

                    return finalize_response(resp)

                # Auto-manage tool_block_pending on after_tool before rule eval.
                if is_after_tool:
                    is_failure = event.metadata.get("is_failure", False) or event.data.get(
                        "is_error", False
                    )
                    if is_failure:
                        variables["tool_block_pending"] = True
                        self._check_catastrophic_failure(event, variables)
                    else:
                        # Snapshot before clearing — if a tool just failed,
                        # a parallel non-edit success shouldn't clear edit state.
                        had_pending_failure = variables.get("tool_block_pending", False)

                        # Clear tool_block_pending on successful tool completion
                        variables["tool_block_pending"] = False
                        variables["_last_blocked_tool"] = ""
                        variables["consecutive_tool_blocks"] = 0

                        # Clear edit_write_pending when the successful tool is an
                        # edit/write, OR when no failure is pending (stale flag).
                        # Don't clear on non-edit success during a parallel failure
                        # — the edit wasn't recovered yet.
                        if variables.get("edit_write_pending"):
                            if _is_write_like_event_data(event.data) or not had_pending_failure:
                                _clear_edit_write_state(variables)

                # 5. Evaluate rules in priority order
                context_parts: list[str] = []
                if _step_transition_msg:
                    context_parts.append(_step_transition_msg)
                block_reason: str | None = None

                for _row, body in rules:
                    # Pre-filter: skip rule if tools field doesn't match current tool
                    if body.tools:
                        tool_name = event.data.get("tool_name", "")
                        if tool_name not in body.tools:
                            continue

                    # Build fresh eval context with current variables
                    ctx = self._build_eval_context(event, variables, eval_context)

                    # Build allowed_funcs once per iteration — shared by condition and templates
                    allowed_funcs = self._build_allowed_funcs(ctx)

                    # Check rule-level `when` condition
                    if body.when:
                        # Use first effect type for fail-open/closed heuristic
                        first_type = (
                            body.resolved_effects[0].type if body.resolved_effects else "block"
                        )
                        if not self._evaluate_condition(body.when, ctx, first_type, allowed_funcs):
                            continue

                    # Process effects: non-block effects first, then block (if any)
                    effects = body.resolved_effects
                    deferred_block: RuleEffect | None = None
                    rule_start = time.perf_counter()
                    rule_blocked = False

                    for effect in effects:
                        if not self._effect_matches_event(effect, event):
                            continue

                        # Check per-effect `when` condition
                        if effect.when:
                            if not self._evaluate_condition(
                                effect.when, ctx, effect.type, allowed_funcs
                            ):
                                continue

                        if effect.type == "block":
                            # Defer block to after all sibling non-block effects
                            deferred_block = effect
                            continue

                        # Apply non-block effects immediately
                        should_continue = await self._apply_effect(
                            effect,
                            _row,
                            variables,
                            ctx,
                            allowed_funcs,
                            context_parts,
                            mcp_calls,
                        )
                        if not should_continue:
                            break  # Inline dispatch failed — skip remaining effects

                    # Now apply deferred block (if any)
                    if deferred_block is not None:
                        if self._effect_matches_event(deferred_block, event):
                            rule_blocked = True
                            block_reason = _ensure_block_reason(
                                session_id=session_id,
                                event=event,
                                source="rule",
                                rule_name=_row.name,
                                reason=deferred_block.reason,
                                fallback_reason=(
                                    "Rule block effect omitted a reason. Update the rule "
                                    "definition to explain why the event was blocked."
                                ),
                                warn_detail="rule block effect omitted a reason",
                            )
                            block_reason = self._render_template(block_reason, ctx, allowed_funcs)
                            block_reason = f"Rule enforced by Gobby: [{_row.name}]\n{block_reason}"
                            # Track the blocked tool so repeated retries can escalate,
                            # but do not mark this as a tool execution failure.
                            if is_before_tool:
                                variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                                # Blocked edit/write never executed — nothing to recover
                                if _is_write_like_event_data(event.data):
                                    _clear_edit_write_state(variables)

                    # Record rule evaluation metric
                    if self._event_store:
                        rule_latency = (time.perf_counter() - rule_start) * 1000
                        try:
                            self._event_store.record_event(
                                event_type="rule_eval",
                                name=_row.name,
                                session_id=session_id,
                                success=not rule_blocked,
                                result="block" if rule_blocked else "allow",
                                latency_ms=rule_latency,
                            )
                        except Exception as e:
                            logger.debug(f"Metrics recording failed: {e}")

                    if rule_blocked:
                        # First block wins — stop evaluating
                        break

                # 6. Build response — overrides take precedence over rule-evaluated decisions,
                # but the rule loop always runs so mcp_calls are always collected.
                ctx_str = "\n\n".join(context_parts) if context_parts else None
                meta = {"mcp_calls": mcp_calls} if mcp_calls else {}

                # Propagate rewrite_input from variables to response
                rewrite_meta = variables.pop("_rewrite_input", None)
                modified_input: dict[str, Any] | None = None
                auto_approve = False
                if rewrite_meta and isinstance(rewrite_meta, dict):
                    modified_input = rewrite_meta.get("input_updates")
                    auto_approve = rewrite_meta.get("auto_approve", False)

                permission_meta = variables.pop("_permission_response", None)
                permission_decision: str | None = None
                updated_permissions: list[dict[str, Any]] | None = None
                if permission_meta and isinstance(permission_meta, dict):
                    if permission_meta.get("input_updates") is not None:
                        modified_input = permission_meta.get("input_updates")
                    permission_decision = permission_meta.get("permission_decision")
                    updated_permissions = permission_meta.get("updated_permissions")

                watch_paths = variables.pop("_watch_paths", None)
                worktree_path = variables.pop("_worktree_path", None)
                retry = bool(variables.pop("_retry", False))
                elicitation_meta = variables.pop("_elicitation", None)
                elicitation_action: str | None = None
                elicitation_content: dict[str, Any] | None = None
                elicitation_error: str | None = None
                if elicitation_meta and isinstance(elicitation_meta, dict):
                    elicitation_action = elicitation_meta.get("action")
                    elicitation_content = elicitation_meta.get("content")
                    elicitation_error = elicitation_meta.get("error")

                response_kwargs = {
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
                    resp = HookResponse(
                        decision="block",
                        reason=override_reason or "",
                        context=ctx_str,
                        **response_kwargs,
                    )
                elif override_decision == "allow":
                    resp = HookResponse(
                        decision="allow",
                        context=ctx_str,
                        **response_kwargs,
                    )
                elif block_reason:
                    resp = HookResponse(
                        decision="block",
                        reason=block_reason,
                        context=ctx_str,
                        **response_kwargs,
                    )
                else:
                    resp = HookResponse(
                        decision="allow",
                        context=ctx_str,
                        **response_kwargs,
                    )

                if span.is_recording():
                    span.set_attribute(
                        "rules.evaluated",
                        [row.name for row, _ in rules],
                    )
                    if mcp_calls:
                        span.set_attribute(
                            "rules.mcp_calls",
                            [f"{c.get('server')}/{c.get('tool')}" for c in mcp_calls],
                        )
                return finalize_response(resp)
            except Exception as e:
                if span.is_recording():
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                raise

    def _load_rules(
        self, rule_events: list[RuleTriggerEvent]
    ) -> list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]]:
        """Load enabled rules matching any trigger event, sorted by priority."""
        ordered: list[tuple[int, WorkflowDefinitionRow, RuleDefinitionBody]] = []
        seen_rows: set[str] = set()

        for trigger_index, rule_event in enumerate(rule_events):
            rows = self.definition_manager.list_rules_by_event(
                event=rule_event.value,
                enabled=True,
            )
            for row in rows:
                if row.id in seen_rows:
                    continue
                try:
                    body = RuleDefinitionBody.model_validate_json(row.definition_json)
                    ordered.append((trigger_index, row, body))
                    seen_rows.add(row.id)
                except Exception as e:
                    logger.warning(f"Failed to parse rule {row.name}: {e}")

        ordered.sort(key=lambda item: (item[1].priority, item[0], item[1].name))
        return [(row, body) for _, row, body in ordered]

    def _load_session_overrides(self, session_id: str) -> dict[str, bool]:
        """Load session-scoped rule overrides."""
        rows = self.db.fetchall(
            "SELECT rule_name, enabled FROM rule_overrides WHERE session_id = ?",
            (session_id,),
        )
        return {row["rule_name"]: bool(row["enabled"]) for row in rows}

    def _apply_overrides(
        self,
        rules: list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]],
        overrides: dict[str, bool],
    ) -> list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]]:
        """Filter rules based on session overrides."""
        if not overrides:
            return rules
        return [
            (row, body)
            for row, body in rules
            if overrides.get(row.name, True)  # Default to enabled if no override
        ]

    def _filter_by_agent_scope(
        self,
        rules: list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]],
        agent_type: str | None,
    ) -> list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]]:
        """Filter rules by agent_scope.

        - Rules with no agent_scope (None) are global — always included.
        - Rules with agent_scope require _agent_type to be in the list.
        - If no _agent_type is set, only global rules are included.
        """
        return [
            (row, body)
            for row, body in rules
            if body.agent_scope is None
            or (
                agent_type is not None
                and ("*" in body.agent_scope or agent_type in body.agent_scope)
            )
        ]

    def _filter_by_active_rules(
        self,
        rules: list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]],
        variables: dict[str, Any],
    ) -> list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]]:
        """Filter rules based on resolved selectors (if any) stored in session variables."""
        active_names = variables.get("_active_rule_names")
        if active_names is None:
            return rules  # no filter — current behavior preserved
        active_set = set(active_names)
        return [(row, body) for row, body in rules if row.name in active_set]
