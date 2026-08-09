"""Effect handling for the rule engine.

Handles applying rule effects: set_variable, inject_context, observe,
mcp_call, rewrite_input, load_skill, run_command, and block matching.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.effect_deadline import (
    BLOCKING_EFFECT_BUDGET_SECONDS,
    remaining_blocking_effect_seconds,
)
from gobby.hooks.events import HookEvent
from gobby.hooks.normalization import is_shell_tool
from gobby.sessions.compact_markers import WORKFLOW_REQUESTED_SKILLS_VARIABLE
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.engine._offload import offload
from gobby.workflows.engine.delivery_formatting import (
    DeliveryFormattingMixin,
    _is_empty_inject_payload,
)
from gobby.workflows.engine.run_command import (
    RunCommandResult,
    build_run_command_payload,
    execute_run_command,
)
from gobby.workflows.reserved_variables import is_internal_rule, is_reserved_workflow_variable
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

logger = logging.getLogger(__name__)

_RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS = 5.0
_RUN_COMMAND_BACKGROUND_DEFAULT_TIMEOUT_SECONDS = 30.0


class EffectsMixin(DeliveryFormattingMixin):
    """Mixin providing effect handling methods for RuleEngine."""

    db: Any
    _skill_manager: Any
    _mcp_dispatcher: Any

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
            await offload(
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
                template_text = await offload(
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

        elif effect.type == "set_display_content":
            variables["_display_content"] = await offload(
                self._render_template,
                effect.template,
                ctx,
                allowed_funcs,
            )

        elif effect.type == "observe":
            obs_list = variables.get("_observations", [])
            msg = effect.message or ""
            msg = await offload(self._render_template, msg, ctx, allowed_funcs)
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
            rendered_args = await offload(
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

                        memory_result_handled = False
                        if isinstance(raw_result, dict) and isinstance(event_obj, HookEvent):
                            memory_result_handled, formatted = await offload(
                                self._format_memory_backed_result,
                                server=effect.server,
                                tool=effect.tool,
                                result=raw_result,
                                event=event_obj,
                                platform_session_id=platform_session_id,
                                variables=variables,
                            )
                        if memory_result_handled:
                            pass
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
                            "Inline mcp_call %s/%s failed (rule %s): %s",
                            effect.server,
                            effect.tool,
                            row.name,
                            error,
                        )
                        if effect.block_on_failure:
                            return (
                                f"Auto-heal prerequisite failed: "
                                f"{effect.server}/{effect.tool}: {error}"
                            )
                except Exception as exc:
                    logger.warning(
                        "Inline mcp_call %s/%s raised (rule %s)",
                        effect.server,
                        effect.tool,
                        row.name,
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
                rendered_updates = await offload(
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
                                "Malformed original_args JSON, defaulting to empty dict: %s", e
                            )
                            original_args = {}
                    if not isinstance(original_args, dict):
                        logger.warning(
                            "original_args is %s, not dict — defaulting to empty dict",
                            type(original_args).__name__,
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
                permission_meta["input_updates"] = await offload(
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
                    variables["_worktree_path"] = await offload(
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
                elicitation_meta["content"] = await offload(
                    self._render_nested_value,
                    effect.elicitation_content,
                    ctx,
                    allowed_funcs,
                )
            if effect.elicitation_error is not None:
                elicitation_meta["error"] = await offload(
                    self._render_nested_value,
                    effect.elicitation_error,
                    ctx,
                    allowed_funcs,
                )

        elif effect.type == "run_command":
            await self._apply_run_command(effect, row, ctx, context_parts)

        elif effect.type == "load_skill":
            if effect.skill:
                from gobby.skills.formatting import skill_fetch_directive

                context_parts.append(skill_fetch_directive(effect.skill))
                # Record the request so compaction can reload it. The directive
                # itself only lives in this turn's context, and a workflow that
                # asked for a skill still needs it after the context is summarized
                # away. Stored as list[str] because session variables are
                # JSON-persisted.
                requested = variables.get(WORKFLOW_REQUESTED_SKILLS_VARIABLE)
                if not isinstance(requested, list):
                    requested = []
                if effect.skill not in requested:
                    requested.append(effect.skill)
                variables[WORKFLOW_REQUESTED_SKILLS_VARIABLE] = requested

        return None

    async def _apply_run_command(
        self,
        effect: Any,
        row: WorkflowDefinitionRow,
        ctx: dict[str, Any],
        context_parts: list[str],
    ) -> None:
        """Execute a bounded detector command and fail open on every failure."""
        event = ctx.get("event")
        command = [str(part) for part in (effect.command or [])]
        if not command or not isinstance(event, HookEvent):
            return

        try:
            payload = build_run_command_payload(event)
            stdin_payload = json.dumps(payload).encode("utf-8")
        except Exception:
            logger.warning("run_command[%s]: event payload could not be encoded", row.name)
            return
        cwd = str(payload["cwd"])
        platform_session_id = event.metadata.get("_platform_session_id") if event.metadata else None
        if not isinstance(platform_session_id, str) or not platform_session_id:
            platform_session_id = None

        if effect.background:
            timeout = effect.timeout_seconds or _RUN_COMMAND_BACKGROUND_DEFAULT_TIMEOUT_SECONDS
            create_background_task(
                self._run_command_then_deliver(
                    command,
                    cwd,
                    stdin_payload,
                    timeout,
                    rule_name=row.name,
                    rule_id=str(row.id),
                    platform_session_id=platform_session_id,
                )
            )
            return

        maximum = min(
            effect.timeout_seconds or _RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS,
            BLOCKING_EFFECT_BUDGET_SECONDS,
        )
        deadline = ctx.get("_blocking_deadline")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            deadline = None
        timeout = remaining_blocking_effect_seconds(deadline, maximum=maximum)
        if timeout <= 0:
            result = RunCommandResult.deadline_exhausted(timeout_seconds=timeout)
            self._audit_run_command(
                result,
                rule_name=row.name,
                rule_id=str(row.id),
                platform_session_id=platform_session_id,
            )
            return
        result = await self._execute_run_command(
            command,
            cwd,
            stdin_payload,
            timeout,
            rule_name=row.name,
            rule_id=str(row.id),
            platform_session_id=platform_session_id,
            background=False,
        )
        if result.context and effect.inject_result:
            context_parts.append(result.context)

    async def _execute_run_command(
        self,
        command: list[str],
        cwd: str,
        stdin_payload: bytes,
        timeout: float,
        *,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
        background: bool,
    ) -> RunCommandResult:
        result = await execute_run_command(
            command,
            cwd=cwd,
            stdin_payload=stdin_payload,
            timeout_seconds=timeout,
            background=background,
        )
        self._audit_run_command(
            result,
            rule_name=rule_name,
            rule_id=rule_id,
            platform_session_id=platform_session_id,
        )
        if result.status != "success":
            logger.warning("run_command[%s]: %s (fail-open)", rule_name, result.status)
        return result

    async def _run_command_then_deliver(
        self,
        command: list[str],
        cwd: str,
        stdin_payload: bytes,
        timeout: float,
        *,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        """Background variant: run the command, deliver output on the next turn."""
        result = await self._execute_run_command(
            command,
            cwd,
            stdin_payload,
            timeout,
            rule_name=rule_name,
            rule_id=rule_id,
            platform_session_id=platform_session_id,
            background=True,
        )
        if not result.context or not platform_session_id:
            return
        try:
            from gobby.storage.inter_session_messages import InterSessionMessageManager

            InterSessionMessageManager(self.db).create_message(
                from_session=platform_session_id,
                to_session=platform_session_id,
                content=result.context,
                message_type="command_result",
            )
        except Exception:
            logger.warning("run_command[%s]: background delivery failed", rule_name, exc_info=True)

    def _audit_run_command(
        self,
        result: RunCommandResult,
        *,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        if not platform_session_id or getattr(self, "db", None) is None:
            return
        try:
            from gobby.storage.workflow_audit import WorkflowAuditManager

            WorkflowAuditManager(self.db).log(
                session_id=platform_session_id,
                step=rule_name,
                event_type="effect",
                result=result.status,
                rule_id=rule_id,
                context={
                    "duration_ms": result.duration_ms,
                    "exit_code": result.exit_code,
                    "stdout_bytes": result.stdout_bytes,
                    "stderr_bytes": result.stderr_bytes,
                    "timeout_seconds": result.timeout_seconds,
                    "overflow_stream": result.overflow_stream,
                    "background": result.background,
                },
            )
        except Exception:
            logger.warning("run_command[%s]: audit write failed", rule_name, exc_info=True)

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
                logger.warning(
                    "Failed to evaluate set_variable expression '%s': %s", effect.value, e
                )
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
