"""Effect handling for the rule engine.

Handles applying rule effects: set_variable, inject_context, observe,
mcp_call, rewrite_input, load_skill, run_command, and block matching.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.effect_deadline import (
    BLOCKING_EFFECT_BUDGET_SECONDS,
    remaining_blocking_effect_seconds,
)
from gobby.hooks.events import HookEvent
from gobby.hooks.normalization import is_shell_tool
from gobby.sessions.compact_markers import WORKFLOW_REQUESTED_SKILLS_VARIABLE
from gobby.skills.materialization import SkillScriptMaterializer
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.enforcement.blocking import is_gobby_call_tool
from gobby.workflows.engine._offload import offload
from gobby.workflows.engine.delivery_formatting import (
    DeliveryFormattingMixin,
    _is_empty_inject_payload,
)
from gobby.workflows.engine.run_command import (
    RunCommandResult,
    build_run_command_payload,
    execute_run_command,
    resolve_materialized_skill_script,
)
from gobby.workflows.reserved_variables import is_internal_rule, is_reserved_workflow_variable
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

logger = logging.getLogger(__name__)

_RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS = 5.0
_RUN_COMMAND_BACKGROUND_DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _PreparedRunCommand:
    command: list[str]
    environment: dict[str, str]
    scripts_dir: Path | None = None
    script: str | None = None


class EffectsMixin(DeliveryFormattingMixin):
    """Mixin providing effect handling methods for RuleEngine."""

    _background_run_commands: dict[tuple[str, str], asyncio.Task[None]]

    def __init__(self) -> None:
        self._background_run_commands = {}

    db: Any
    _skill_manager: Any
    _mcp_dispatcher: Any
    skill_script_materializer: SkillScriptMaterializer

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
        row: RuleDefinitionRow,
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
                # Injected-context fencing lives in the handoff templates
                # themselves, not here, so per-turn injections (brevity, memory,
                # task context) stay un-tagged. See
                # context-handoff/inject-compact-handoff.yaml and
                # context-handoff/inject-clear-handoff.yaml.
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
                if event and is_gobby_call_tool(event.data.get("tool_name")):
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
        row: RuleDefinitionRow,
        ctx: dict[str, Any],
        context_parts: list[str],
    ) -> None:
        """Execute a bounded detector command and fail open on every failure."""
        event = ctx.get("event")
        if not isinstance(event, HookEvent):
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
            registry = self._background_run_command_registry()
            key = (event.session_id, str(row.id))
            existing = registry.get(key)
            if existing is not None and not existing.done():
                logger.info(
                    "run_command[%s]: suppressed duplicate background run for session %s",
                    row.name,
                    event.session_id,
                    extra={"rule_name": row.name, "session_id": event.session_id},
                )
                return
            task = create_background_task(
                self._run_command_then_deliver(
                    [str(part) for part in (effect.command or [])],
                    cwd,
                    stdin_payload,
                    timeout,
                    project_id=event.project_id,
                    skill=effect.skill,
                    script=effect.script,
                    rule_name=row.name,
                    rule_id=str(row.id),
                    platform_session_id=platform_session_id,
                )
            )
            registry[key] = task

            def cleanup(completed: asyncio.Task[None]) -> None:
                if registry.get(key) is completed:
                    registry.pop(key, None)

            task.add_done_callback(cleanup)
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
            result = RunCommandResult.deadline_exhausted(
                timeout_seconds=max(timeout, 0.0),
                skill=effect.skill,
                script=effect.script,
            )
            await self._record_run_command_failure(
                result,
                rule_name=row.name,
                rule_id=str(row.id),
                platform_session_id=platform_session_id,
            )
            return
        started = time.perf_counter()
        prepared = await self._prepare_run_command(
            [str(part) for part in (effect.command or [])],
            project_id=event.project_id,
            skill=effect.skill,
            script=effect.script,
            timeout=timeout,
            background=False,
        )
        if isinstance(prepared, RunCommandResult):
            await self._record_run_command_failure(
                prepared,
                rule_name=row.name,
                rule_id=str(row.id),
                platform_session_id=platform_session_id,
            )
            return
        timeout -= time.perf_counter() - started
        if timeout <= 0:
            result = RunCommandResult.deadline_exhausted(
                timeout_seconds=max(timeout, 0.0),
                skill=effect.skill,
                script=effect.script,
            )
            await self._record_run_command_failure(
                result,
                rule_name=row.name,
                rule_id=str(row.id),
                platform_session_id=platform_session_id,
            )
            return
        result = await self._execute_run_command(
            prepared,
            cwd,
            stdin_payload,
            timeout,
            skill=effect.skill,
            script=effect.script,
            rule_name=row.name,
            rule_id=str(row.id),
            platform_session_id=platform_session_id,
            background=False,
        )
        if result.context and effect.inject_result:
            context_parts.append(result.context)

    async def _prepare_run_command(
        self,
        command: list[str],
        *,
        project_id: str | None,
        skill: str | None,
        script: str | None,
        timeout: float,
        background: bool,
    ) -> _PreparedRunCommand | RunCommandResult:
        if skill is None or script is None:
            return _PreparedRunCommand(command, {})

        started = time.perf_counter()
        try:
            materialized = await asyncio.wait_for(
                self.skill_script_materializer.resolve(skill, project_id=project_id),
                timeout=timeout,
            )
            resolve_materialized_skill_script(materialized.scripts_dir, script)
        except TimeoutError:
            return RunCommandResult.skill_resolution_failure(
                "skill_resolution_timeout",
                started=started,
                timeout_seconds=timeout,
                background=background,
                skill=skill,
                script=script,
            )
        except Exception:
            logger.debug(
                "run_command: skill resolution failed",
                extra={"skill": skill, "script": script},
                exc_info=True,
            )
            return RunCommandResult.skill_resolution_failure(
                "skill_resolution_error",
                started=started,
                timeout_seconds=timeout,
                background=background,
                skill=skill,
                script=script,
            )
        return _PreparedRunCommand(
            command,
            materialized.environment,
            scripts_dir=materialized.scripts_dir,
            script=script,
        )

    async def _execute_run_command(
        self,
        prepared: _PreparedRunCommand,
        cwd: str,
        stdin_payload: bytes,
        timeout: float,
        *,
        skill: str | None,
        script: str | None,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
        background: bool,
    ) -> RunCommandResult:
        spawn_guard: AbstractAsyncContextManager[None] | None = None
        command_factory: Callable[[], list[str]] | None = None
        if prepared.scripts_dir is not None and prepared.script is not None:
            scripts_dir = prepared.scripts_dir
            materialized_script = prepared.script
            spawn_guard = self.skill_script_materializer.execution_guard(scripts_dir)

            def resolve_command() -> list[str]:
                target = resolve_materialized_skill_script(scripts_dir, materialized_script)
                return [*prepared.command, str(target)]

            command_factory = resolve_command

        result = replace(
            await execute_run_command(
                prepared.command,
                cwd=cwd,
                stdin_payload=stdin_payload,
                timeout_seconds=timeout,
                background=background,
                environment=prepared.environment,
                spawn_guard=spawn_guard,
                command_factory=command_factory,
            ),
            skill=skill,
            script=script,
        )
        if result.status == "success":
            await self._audit_run_command(
                result,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
        else:
            await self._record_run_command_failure(
                result,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
        return result

    async def _run_command_then_deliver(
        self,
        command: list[str],
        cwd: str,
        stdin_payload: bytes,
        timeout: float,
        *,
        project_id: str | None,
        skill: str | None,
        script: str | None,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        """Background variant: run the command, deliver output on the next turn."""
        started = time.perf_counter()
        prepared = await self._prepare_run_command(
            command,
            project_id=project_id,
            skill=skill,
            script=script,
            timeout=timeout,
            background=True,
        )
        if isinstance(prepared, RunCommandResult):
            await self._record_run_command_failure(
                prepared,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
            return
        timeout -= time.perf_counter() - started
        if timeout <= 0:
            result = RunCommandResult.deadline_exhausted(
                timeout_seconds=max(timeout, 0.0),
                background=True,
                skill=skill,
                script=script,
            )
            await self._record_run_command_failure(
                result,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
            return
        result = await self._execute_run_command(
            prepared,
            cwd,
            stdin_payload,
            timeout,
            skill=skill,
            script=script,
            rule_name=rule_name,
            rule_id=rule_id,
            platform_session_id=platform_session_id,
            background=True,
        )
        if not result.context or not platform_session_id:
            return
        try:
            from gobby.storage.inter_session_messages import InterSessionMessageManager

            manager = InterSessionMessageManager(self.db)
            await offload(
                manager.create_message,
                from_session=platform_session_id,
                to_session=platform_session_id,
                content=result.context,
                message_type="command_result",
            )
        except Exception:
            logger.warning("run_command[%s]: background delivery failed", rule_name, exc_info=True)

    async def _record_run_command_failure(
        self,
        result: RunCommandResult,
        *,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        await self._audit_run_command(
            result,
            rule_name=rule_name,
            rule_id=rule_id,
            platform_session_id=platform_session_id,
        )
        logger.warning(
            "run_command[%s]: %s phase=%s skill=%s script=%s (fail-open)",
            rule_name,
            result.status,
            result.phase,
            result.skill,
            result.script,
        )

    async def _audit_run_command(
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

            manager = WorkflowAuditManager(self.db)
            await offload(
                manager.log,
                session_id=platform_session_id,
                step=rule_name,
                event_type="effect",
                result=result.status,
                rule_id=rule_id,
                context={
                    "phase": result.phase,
                    "skill": result.skill,
                    "script": result.script,
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

    def _background_run_command_registry(
        self,
    ) -> dict[tuple[str, str], asyncio.Task[None]]:
        return self._background_run_commands

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
