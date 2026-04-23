"""Effect handling for the rule engine.

Handles applying rule effects: set_variable, inject_context, observe,
mcp_call, rewrite_input, load_skill, and block matching.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent
from gobby.hooks.normalization import is_shell_tool
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

logger = logging.getLogger(__name__)


class EffectsMixin:
    """Mixin providing effect handling methods for RuleEngine."""

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
    ) -> bool:
        """Apply a single non-block effect.

        Returns:
            True to continue processing sibling effects, False to abort
            remaining effects for this rule (e.g. inline mcp_call failed).
        """
        if effect.type == "set_variable":
            self._apply_set_variable(effect, variables, ctx)

        elif effect.type == "inject_context":
            # NOTE: inject_context templates render with rule evaluation context:
            # event, variables (flattened to top-level), and helper functions.
            # Session data (summary_markdown, task_context) is populated as session
            # variables by the SESSION_START handler before rules evaluate, making
            # them available as {{ session_summary }}, {{ task_context }} in templates.
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
                    if success and dr.get("result"):
                        from gobby.hooks.dispatchers.mcp import format_discovery_result

                        formatted = format_discovery_result(
                            {"tool": effect.tool, "result": dr["result"]}
                        )
                        if formatted:
                            context_parts.append(formatted)
                    elif not success:
                        error = dr.get("result", {}).get("error", "unknown") if dr else "no result"
                        logger.warning(
                            f"Inline mcp_call {effect.server}/{effect.tool} failed "
                            f"(rule {row.name}): {error} — aborting remaining effects",
                        )
                        return False
                except Exception:
                    logger.warning(
                        f"Inline mcp_call {effect.server}/{effect.tool} raised "
                        f"(rule {row.name}) — aborting remaining effects",
                        exc_info=True,
                    )
                    return False
                return True

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
                rendered_updates = self._render_nested_value(
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
                permission_meta["input_updates"] = self._render_nested_value(
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
                    variables["_worktree_path"] = self._render_nested_value(
                        effect.worktree_path,
                        ctx,
                        allowed_funcs,
                    )

        elif effect.type == "set_elicitation":
            elicitation_meta = variables.setdefault("_elicitation", {})
            if effect.elicitation_action:
                elicitation_meta["action"] = effect.elicitation_action
            if effect.elicitation_content is not None:
                elicitation_meta["content"] = self._render_nested_value(
                    effect.elicitation_content,
                    ctx,
                    allowed_funcs,
                )
            if effect.elicitation_error is not None:
                elicitation_meta["error"] = self._render_nested_value(
                    effect.elicitation_error,
                    ctx,
                    allowed_funcs,
                )

        elif effect.type == "load_skill":
            if effect.skill:
                from gobby.skills.formatting import skill_fetch_directive

                context_parts.append(skill_fetch_directive(effect.skill))

        return True

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
    ) -> None:
        """Apply a set_variable effect, handling expressions."""
        if effect.variable is None:
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
