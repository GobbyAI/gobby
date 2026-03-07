"""Rule engine with single-pass evaluation loop.

Rules are stateless event handlers: event comes in, conditions match, effect fires.
Five effect types: block, set_variable, inject_context, mcp_call, observe.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pydantic

from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.definitions import (
    RuleDefinitionBody,
    RuleEffect,
    RuleEvent,
    WorkflowDefinition,
    WorkflowStep,
)
from gobby.workflows.enforcement.blocking import (
    is_discovery_tool,
    is_message_delivery_tool,
    is_plan_file,
    is_server_listed,
    is_tool_unlocked,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.state_manager import WorkflowInstanceManager
from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)

# Map HookEventType to RuleEvent
_EVENT_TYPE_MAP: dict[HookEventType, RuleEvent] = {
    HookEventType.BEFORE_TOOL: RuleEvent.BEFORE_TOOL,
    HookEventType.AFTER_TOOL: RuleEvent.AFTER_TOOL,
    HookEventType.BEFORE_AGENT: RuleEvent.BEFORE_AGENT,
    HookEventType.SESSION_START: RuleEvent.SESSION_START,
    HookEventType.SESSION_END: RuleEvent.SESSION_END,
    HookEventType.STOP: RuleEvent.STOP,
    HookEventType.PRE_COMPACT: RuleEvent.PRE_COMPACT,
}


class RuleEngine:
    """Single-pass rule evaluation engine.

    Loads rules from workflow_definitions (workflow_type='rule'),
    applies session overrides, evaluates in priority order.
    """

    def __init__(self, db: DatabaseProtocol):
        self.db = db
        self.definition_manager = LocalWorkflowDefinitionManager(db)
        self.instance_manager = WorkflowInstanceManager(db)

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
        rule_event = _EVENT_TYPE_MAP.get(event.event_type)
        if rule_event is None:
            return HookResponse(decision="allow")

        # Check global enforcement toggle
        config_store = ConfigStore(self.db)
        if config_store.get("rules.enforcement_enabled") is False:
            return HookResponse(decision="allow")

        # Auto-track consecutive tool blocks (universal safety — not configurable)
        # Only escalate when the SAME tool is retried — different tools reset the counter
        # so the agent can recover by using other tools (Read, Bash, etc.).
        if rule_event == RuleEvent.BEFORE_TOOL and variables.get("tool_block_pending"):
            tool_name = event.data.get("tool_name", "")
            last_blocked = variables.get("_last_blocked_tool", "")
            if tool_name == last_blocked:
                count = variables.get("consecutive_tool_blocks", 0) + 1
                variables["consecutive_tool_blocks"] = count
                if count >= 2:
                    return HookResponse(
                        decision="block",
                        reason=(
                            "Rule enforced by Gobby: [consecutive-tool-block]\n"
                            f"You have attempted {tool_name} {count + 1} times consecutively "
                            "without addressing the error.\n"
                            "STOP retrying the same action. Read the previous error messages "
                            "and take a DIFFERENT action to resolve the underlying issue first."
                        ),
                    )
            else:
                # Different tool — reset counter, let it through to rule evaluation
                variables["consecutive_tool_blocks"] = 0
        elif rule_event == RuleEvent.BEFORE_AGENT:
            variables["consecutive_tool_blocks"] = 0
            variables["_last_blocked_tool"] = ""
            variables["tool_block_pending"] = False
            variables["stop_attempts"] = 0

        # Auto-increment stop attempts (universal — not configurable)
        if rule_event == RuleEvent.STOP:
            variables["stop_attempts"] = variables.get("stop_attempts", 0) + 1
            logger.debug(
                "STOP gate diagnostics: session_id=%s, auto_task_ref=%r, "
                "stop_attempts=%s, task_claimed=%s, claimed_tasks=%s, "
                "pre_existing_errors_triaged=%s, error_triage_blocks=%s",
                session_id,
                variables.get("auto_task_ref"),
                variables["stop_attempts"],
                variables.get("task_claimed"),
                variables.get("claimed_tasks"),
                variables.get("pre_existing_errors_triaged"),
                variables.get("error_triage_blocks", 0),
            )

        # 1. Load enabled rules for this event, sorted by priority
        rules = self._load_rules(rule_event)

        # 2. Apply session overrides
        overrides = self._load_session_overrides(session_id)
        rules = self._apply_overrides(rules, overrides)

        # 3. Filter by agent_scope
        agent_type = variables.get("_agent_type")
        rules = self._filter_by_agent_scope(rules, agent_type)

        # 4. Filter by active rules (selector-based)
        rules = self._filter_by_active_rules(rules, variables)

        # 4b. Step-level tool enforcement (preempts declarative rules)
        if rule_event == RuleEvent.BEFORE_TOOL:
            step_block = self._check_step_tool_enforcement(event, session_id)
            if step_block is not None:
                variables["tool_block_pending"] = True
                variables["_last_blocked_tool"] = event.data.get("tool_name", "")
                return step_block

        # 4c. Step workflow transition processing (after successful MCP tool calls)
        if rule_event == RuleEvent.AFTER_TOOL:
            self._process_step_after_tool(event, session_id, variables)

        # Deferred overrides — these used to early-return, but that skipped rule
        # evaluation entirely, preventing mcp_call effects (like digest-on-response)
        # from being collected. Now we record the override and let the loop run.
        override_decision: str | None = None
        override_reason: str | None = None

        # Force-allow stop (catastrophic failure bypass — self-clearing)
        if rule_event == RuleEvent.STOP and variables.get("force_allow_stop"):
            variables["force_allow_stop"] = False
            override_decision = "allow"

        # Auto-block stop when a tool just failed (self-clearing)
        elif rule_event == RuleEvent.STOP and variables.get("tool_block_pending"):
            variables["tool_block_pending"] = False
            override_decision = "block"
            override_reason = (
                "Rule enforced by Gobby: [tool-failure-recovery]\n"
                "A tool just failed. Read the error and recover — do not stop."
            )

        if not rules:
            # Auto-manage tool_block_pending on after_tool
            # (Symmetric with auto-set on before_tool block at line ~164)
            if rule_event == RuleEvent.AFTER_TOOL:
                is_failure = event.metadata.get("is_failure", False) or event.data.get(
                    "is_error", False
                )
                if is_failure:
                    variables["tool_block_pending"] = True
                    self._check_catastrophic_failure(event, variables)
                else:
                    if variables.get("tool_block_pending"):
                        variables["tool_block_pending"] = False
                        variables["consecutive_tool_blocks"] = 0
                        variables["_last_blocked_tool"] = ""
            # Honour hardcoded override decisions (e.g. tool_block_pending stop gate)
            # even when no declarative rules are installed for this event.
            if override_decision == "block":
                return HookResponse(decision="block", reason=override_reason or "")
            if override_decision == "allow":
                return HookResponse(decision="allow")
            return HookResponse(decision="allow")

        # Auto-manage tool_block_pending on after_tool before rule eval
        # (Symmetric with auto-set on before_tool block at line ~164)
        if rule_event == RuleEvent.AFTER_TOOL:
            is_failure = event.metadata.get("is_failure", False) or event.data.get(
                "is_error", False
            )
            if is_failure:
                variables["tool_block_pending"] = True
                self._check_catastrophic_failure(event, variables)
            else:
                if variables.get("tool_block_pending"):
                    variables["tool_block_pending"] = False
                    variables["consecutive_tool_blocks"] = 0
                    variables["_last_blocked_tool"] = ""

        # 5. Evaluate rules in priority order
        context_parts: list[str] = []
        mcp_calls: list[dict[str, Any]] = []
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
                first_type = body.resolved_effects[0].type if body.resolved_effects else "block"
                if not self._evaluate_condition(body.when, ctx, first_type, allowed_funcs):
                    continue

            # Process effects: non-block effects first, then block (if any)
            effects = body.resolved_effects
            deferred_block: RuleEffect | None = None

            for effect in effects:
                # Check per-effect `when` condition
                if effect.when:
                    if not self._evaluate_condition(effect.when, ctx, effect.type, allowed_funcs):
                        continue

                if effect.type == "block":
                    # Defer block to after all sibling non-block effects
                    deferred_block = effect
                    continue

                # Apply non-block effects immediately
                self._apply_effect(
                    effect, _row, variables, ctx, allowed_funcs, context_parts, mcp_calls
                )

            # Now apply deferred block (if any)
            if deferred_block is not None:
                if self._should_block(deferred_block, event):
                    block_reason = deferred_block.reason or "Blocked by rule"
                    block_reason = self._render_template(block_reason, ctx, allowed_funcs)
                    block_reason = f"Rule enforced by Gobby: [{_row.name}]\n{block_reason}"
                    # Auto-set tool_block_pending on before_tool blocks
                    if rule_event == RuleEvent.BEFORE_TOOL:
                        variables["tool_block_pending"] = True
                        variables["_last_blocked_tool"] = event.data.get("tool_name", "")
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

        # Propagate compress_output directive to metadata
        compress_meta = variables.pop("_compress_output", None)
        if compress_meta and isinstance(compress_meta, dict):
            meta["compression"] = compress_meta

        if override_decision == "block":
            return HookResponse(
                decision="block",
                reason=override_reason or "",
                context=ctx_str,
                metadata=meta,
            )
        if override_decision == "allow":
            return HookResponse(
                decision="allow",
                context=ctx_str,
                metadata=meta,
                modified_input=modified_input,
                auto_approve=auto_approve,
            )

        if block_reason:
            return HookResponse(
                decision="block",
                reason=block_reason,
                context=ctx_str,
                metadata=meta,
            )

        return HookResponse(
            decision="allow",
            context=ctx_str,
            metadata=meta,
            modified_input=modified_input,
            auto_approve=auto_approve,
        )

    def _load_rules(
        self, rule_event: RuleEvent
    ) -> list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]]:
        """Load enabled rules matching the event type, sorted by priority."""
        rows = self.definition_manager.list_rules_by_event(
            event=rule_event.value,
            enabled=True,
        )
        result: list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]] = []
        for row in rows:
            try:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                result.append((row, body))
            except Exception as e:
                logger.warning(f"Failed to parse rule {row.name}: {e}")
        return result

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

    def _apply_effect(
        self,
        effect: Any,
        row: WorkflowDefinitionRow,
        variables: dict[str, Any],
        ctx: dict[str, Any],
        allowed_funcs: dict[str, Callable[..., Any]],
        context_parts: list[str],
        mcp_calls: list[dict[str, Any]],
    ) -> None:
        """Apply a single non-block effect."""
        if effect.type == "set_variable":
            self._apply_set_variable(effect, variables, ctx)

        elif effect.type == "inject_context":
            # NOTE: inject_context templates render with rule evaluation context:
            # event, variables (flattened to top-level), and helper functions.
            # Session data (summary_markdown, compact_markdown, task_context) is
            # populated as session variables by the SESSION_START handler before
            # rules evaluate, making them available as {{ full_session_summary }},
            # {{ compact_session_summary }}, {{ task_context }} in templates.
            if effect.template:
                template_text = self._render_template(effect.template, ctx, allowed_funcs)
                context_parts.append(template_text)

        elif effect.type == "observe":
            obs_list = variables.get("_observations", [])
            msg = effect.message or ""
            msg = self._render_template(msg, ctx, allowed_funcs)
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
            rendered_args = {
                k: self._render_template(v, ctx, allowed_funcs) if isinstance(v, str) else v
                for k, v in raw_args.items()
            }
            mcp_calls.append(
                {
                    "server": effect.server,
                    "tool": effect.tool,
                    "arguments": rendered_args,
                    "background": effect.background,
                }
            )

        elif effect.type == "rewrite_input":
            if effect.input_updates:
                rendered_updates = {
                    k: self._render_template(v, ctx, allowed_funcs) if isinstance(v, str) else v
                    for k, v in effect.input_updates.items()
                }
                # For MCP call_tool, nest updates inside arguments
                # (mirrors the unwrapping in _build_eval_context)
                event = ctx.get("event")
                if event and event.data.get("tool_name") in ("call_tool", "mcp__gobby__call_tool"):
                    original_args = event.data.get("tool_input", {}).get("arguments", {})
                    if isinstance(original_args, str):
                        try:
                            original_args = json.loads(original_args)
                        except (json.JSONDecodeError, TypeError):
                            original_args = {}
                    if not isinstance(original_args, dict):
                        original_args = {}
                    rendered_updates = {"arguments": {**original_args, **rendered_updates}}
                rewrite_meta = variables.setdefault("_rewrite_input", {})
                rewrite_meta["input_updates"] = rendered_updates
                rewrite_meta["auto_approve"] = effect.auto_approve

        elif effect.type == "compress_output":
            variables["_compress_output"] = {
                "strategy": effect.strategy,
                "max_lines": effect.max_lines,
            }

    def _build_eval_context(
        self,
        event: HookEvent,
        variables: dict[str, Any],
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build evaluation context for condition checking."""
        raw_tool_input = event.data.get("tool_input") or event.data.get("arguments") or {}

        # For MCP call_tool, unwrap nested arguments so rule conditions
        # can reference inner tool params (commit_sha, reason, etc.) directly.
        # Preserve MCP routing fields (server_name, tool_name) so helpers like
        # is_tool_unlocked / is_discovery_tool still work after unwrapping.
        tool_name = event.data.get("tool_name", "")
        if tool_name in ("call_tool", "mcp__gobby__call_tool") and isinstance(raw_tool_input, dict):
            original_tool_input = raw_tool_input
            inner_args = raw_tool_input.get("arguments")
            if isinstance(inner_args, str):
                try:
                    parsed = json.loads(inner_args)
                    if isinstance(parsed, dict):
                        raw_tool_input = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            elif isinstance(inner_args, dict):
                raw_tool_input = inner_args
            # Re-inject MCP routing fields so rule conditions can still access them
            for field in ("server_name", "tool_name"):
                if field in original_tool_input and field not in raw_tool_input:
                    raw_tool_input[field] = original_tool_input[field]

        ctx: dict[str, Any] = {
            "variables": variables,
            "event": event,
            "tool_input": raw_tool_input,
            "source": event.source.value if event.source else None,
        }

        # Flatten variables at top level for convenience
        for key, val in variables.items():
            if key not in ctx:
                ctx[key] = val

        # Add extra context (LazyBool thunks, etc.)
        if extra_context:
            ctx.update(extra_context)

        return ctx

    def _build_allowed_funcs(self, ctx: dict[str, Any]) -> dict[str, Callable[..., Any]]:
        """Build the shared helper-function dict for condition evaluation and template rendering."""
        variables = ctx.get("variables", {})
        funcs = build_condition_helpers(context=ctx)
        funcs["isinstance"] = isinstance
        funcs["is_server_listed"] = lambda ti: is_server_listed(ti, variables)
        funcs["is_tool_unlocked"] = lambda ti: is_tool_unlocked(ti, variables)
        funcs["is_discovery_tool"] = is_discovery_tool
        funcs["is_plan_file"] = is_plan_file
        funcs["is_message_delivery_tool"] = is_message_delivery_tool
        funcs["has_pending_messages"] = self._has_pending_messages
        funcs["pending_message_count"] = self._pending_message_count
        return funcs

    def _render_template(
        self, template: str, ctx: dict[str, Any], allowed_funcs: dict[str, Callable[..., Any]]
    ) -> str:
        """Render a Jinja2 template string with eval context and helper functions."""
        if "{{" not in template:
            return template
        try:
            render_ctx = {**ctx, **allowed_funcs}
            engine = TemplateEngine()
            return engine.render(template, render_ctx)
        except Exception as e:
            logger.warning(f"Failed to render template: {e}")
            return template

    def _has_pending_messages(self, session_id: str) -> bool:
        """Index probe: are there any undelivered messages for this session?"""
        if not session_id:
            return False
        row = self.db.fetchone(
            "SELECT 1 FROM inter_session_messages "
            "WHERE to_session = ? AND delivered_at IS NULL LIMIT 1",
            (session_id,),
        )
        return row is not None

    def _pending_message_count(self, session_id: str) -> int:
        """O(n) count of undelivered messages — only called when a block fires."""
        if not session_id:
            return 0
        row = self.db.fetchone(
            "SELECT COUNT(*) FROM inter_session_messages "
            "WHERE to_session = ? AND delivered_at IS NULL",
            (session_id,),
        )
        return row[0] if row else 0

    # Patterns indicating unrecoverable failures where the agent should stop immediately
    _CATASTROPHIC_PATTERNS = [
        "out of usage",
        "rate limit",
        "quota exceeded",
        "billing",
        "account suspended",
    ]

    def _check_catastrophic_failure(self, event: HookEvent, variables: dict[str, Any]) -> None:
        """Check if a tool failure is catastrophic and set force_allow_stop if so."""
        tool_output = str(event.data.get("tool_output", "")).lower()
        if any(p in tool_output for p in self._CATASTROPHIC_PATTERNS):
            variables["force_allow_stop"] = True

    def _evaluate_condition(
        self,
        condition: str,
        context: dict[str, Any],
        effect_type: str = "block",
        allowed_funcs: dict[str, Callable[..., Any]] | None = None,
    ) -> bool:
        """Evaluate a `when` condition string using SafeExpressionEvaluator.

        On evaluation failure:
        - block effects fail closed (True) — conservative, prevents action
        - other effects fail open (False) — avoids corrupting state or firing unwanted calls
        """
        try:
            if allowed_funcs is None:
                allowed_funcs = self._build_allowed_funcs(context)

            evaluator = SafeExpressionEvaluator(
                context=context,
                allowed_funcs=allowed_funcs,
            )
            return evaluator.evaluate(condition)
        except Exception as e:
            fail_closed = effect_type == "block"
            logger.error(
                f"Failed to evaluate condition '{condition}': {e} "
                f"(defaulting to {'True' if fail_closed else 'False'} for {effect_type} effect)"
            )
            return fail_closed

    def _should_block(self, effect: Any, event: HookEvent) -> bool:
        """Check if a block effect matches the current tool/event."""
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
            if tool_name in effect.tools:
                # Check command patterns for Bash tool
                if tool_name == "Bash" and effect.command_pattern and command:
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

    def _apply_set_variable(
        self,
        effect: Any,
        variables: dict[str, Any],
        eval_context: dict[str, Any],
    ) -> None:
        """Apply a set_variable effect, handling expressions."""
        if effect.variable is None:
            return

        value = effect.value

        # If value is a string that looks like an expression, evaluate it
        if isinstance(value, str) and self._is_expression(value):
            try:
                evaluator = SafeExpressionEvaluator(
                    context=eval_context,
                    allowed_funcs={
                        "len": len,
                        "str": str,
                        "int": int,
                        "bool": bool,
                    },
                )
                value = evaluator.evaluate_value(value)
            except Exception as e:
                logger.warning(f"Failed to evaluate set_variable expression '{effect.value}': {e}")
                return

        variables[effect.variable] = value

    def _is_expression(self, value: str) -> bool:
        """Heuristic: is this string an expression rather than a literal?"""
        expression_indicators = (
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
        return any(indicator in value for indicator in expression_indicators)

    # --- Step workflow enforcement ---

    def _get_step_for_session(
        self, session_id: str
    ) -> tuple[WorkflowStep | None, Any | None, WorkflowDefinition | None]:
        """Get the current workflow step, instance, and definition for a session.

        Returns (step, instance, definition) or (None, None, None) if no active step workflow.
        """
        if not session_id:
            return None, None, None
        instances = self.instance_manager.get_active_instances(session_id)

        for instance in instances:
            if not instance.current_step:
                continue
            row = self.definition_manager.get_by_name(instance.workflow_name)
            if not row or row.workflow_type == "pipeline":
                continue
            try:
                data = json.loads(row.definition_json)
                definition = WorkflowDefinition(**data)
            except (json.JSONDecodeError, pydantic.ValidationError):
                continue
            step = definition.get_step(instance.current_step)
            if step is not None:
                return step, instance, definition
        return None, None, None

    def _check_step_tool_enforcement(
        self, event: HookEvent, session_id: str
    ) -> HookResponse | None:
        """Check step-level tool restrictions. Returns block response or None to continue."""
        step, instance, _defn = self._get_step_for_session(session_id)
        if step is None or instance is None:
            return None

        tool_name = event.data.get("tool_name", "")
        wf_name = instance.workflow_name

        # Discovery tools always pass — agents need progressive discovery in every step
        if tool_name.startswith("mcp__gobby__"):
            mcp_suffix = tool_name[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix):
                return None

        # Check native tool allow-list
        if step.allowed_tools != "all":
            if tool_name not in step.allowed_tools:
                return HookResponse(
                    decision="block",
                    reason=(
                        f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                        f"Tool '{tool_name}' is not allowed in the '{step.name}' step.\n"
                        f"Allowed tools: {', '.join(step.allowed_tools)}"
                    ),
                )

        # Check native tool block-list
        if tool_name in step.blocked_tools:
            return HookResponse(
                decision="block",
                reason=(
                    f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                    f"Tool '{tool_name}' is blocked in the '{step.name}' step."
                ),
            )

        # Check MCP tool restrictions (for call_tool)
        if tool_name in ("call_tool", "mcp__gobby__call_tool"):
            tool_input = event.data.get("tool_input") or {}
            if isinstance(tool_input, dict):
                mcp_server = tool_input.get("server_name", "")
                mcp_tool_name = tool_input.get("tool_name", "")

                # Discovery MCP tools always pass
                if is_discovery_tool(mcp_tool_name):
                    return None

                mcp_key = f"{mcp_server}:{mcp_tool_name}" if mcp_server and mcp_tool_name else ""

                if mcp_key and step.allowed_mcp_tools != "all":
                    if not self._mcp_tool_matches(mcp_key, step.allowed_mcp_tools):
                        return HookResponse(
                            decision="block",
                            reason=(
                                f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                                f"MCP tool '{mcp_key}' is not allowed in the '{step.name}' step.\n"
                                f"Allowed MCP tools: {', '.join(step.allowed_mcp_tools)}"
                            ),
                        )

                if mcp_key and step.blocked_mcp_tools:
                    if self._mcp_tool_matches(mcp_key, step.blocked_mcp_tools):
                        return HookResponse(
                            decision="block",
                            reason=(
                                f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                                f"MCP tool '{mcp_key}' is blocked in the '{step.name}' step."
                            ),
                        )

        return None

    @staticmethod
    def _mcp_tool_matches(mcp_key: str, patterns: list[str]) -> bool:
        """Check if an MCP tool key (server:tool) matches any pattern in the list."""
        for pattern in patterns:
            if pattern == mcp_key:
                return True
            # Wildcard: "server:*"
            if pattern.endswith(":*") and mcp_key.startswith(pattern[:-1]):
                return True
        return False

    def _process_step_after_tool(
        self, event: HookEvent, session_id: str, variables: dict[str, Any]
    ) -> None:
        """Process step workflow on_mcp_success handlers and transitions after tool completion."""
        step, instance, definition = self._get_step_for_session(session_id)
        if step is None or instance is None or definition is None:
            return

        # Only process successful MCP tool completions
        is_failure = event.metadata.get("is_failure", False) or event.data.get("is_error", False)
        if is_failure:
            return

        tool_name = event.data.get("tool_name", "")
        if tool_name not in ("call_tool", "mcp__gobby__call_tool"):
            return

        tool_input = event.data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return

        mcp_server = tool_input.get("server_name", "")
        mcp_tool_name = tool_input.get("tool_name", "")
        if not mcp_server or not mcp_tool_name:
            return

        instance_mgr = WorkflowInstanceManager(self.db)
        vars_changed = False

        # Execute on_mcp_success handlers
        for handler in step.on_mcp_success:
            if handler.get("server") == mcp_server and handler.get("tool") == mcp_tool_name:
                if handler.get("action") == "set_variable":
                    var_name = handler.get("variable")
                    var_value = handler.get("value")
                    if var_name is not None:
                        instance.variables[var_name] = var_value
                        variables[var_name] = var_value
                        vars_changed = True

        # Evaluate transitions
        for transition in step.transitions:
            ctx = {"vars": instance.variables, "variables": variables}
            if not transition.when or self._evaluate_condition(
                transition.when, ctx, "set_variable"
            ):
                old_step = instance.current_step
                new_step = transition.to

                if not definition.get_step(new_step):
                    logger.warning(
                        "Transition to unknown step '%s' in workflow '%s'",
                        new_step,
                        instance.workflow_name,
                    )
                    return

                instance.current_step = new_step
                instance.step_action_count = 0
                instance.step_entered_at = datetime.now(UTC)
                instance_mgr.save_instance(instance)

                logger.info(
                    "Step transition: %s -> %s (workflow=%s, session=%s)",
                    old_step,
                    new_step,
                    instance.workflow_name,
                    session_id,
                )
                return  # First matching transition wins

        # Save if variables changed without transition
        if vars_changed:
            instance_mgr.save_instance(instance)
