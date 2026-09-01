"""Templating and condition evaluation for the rule engine.

Handles building eval context, Jinja2 rendering, SafeExpressionEvaluator
integration, and helper function construction.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import psycopg
from jinja2.exceptions import SecurityError

from gobby.hooks.events import HookEvent
from gobby.skills.formatting import skill_fetch_batch_directive, skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_resolution import is_session_uuid
from gobby.workflows.enforcement.blocking import (
    claimed_task_source_code_write,
    get_touched_file_paths,
    is_current_plan_artifact,
    is_discovery_tool,
    is_gobby_call_tool,
    is_message_delivery_tool,
    is_operator_tool,
    is_plan_file,
    is_tool_unlocked,
    plan_write_paths_allowed,
    requires_task_for_any_touched_file,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)


class TemplatingMixin:
    """Mixin providing templating and condition evaluation methods for RuleEngine."""

    db: HubDatabase

    def _resolve_project_info(
        self,
        event: HookEvent,
        project_from_vars: Any = None,
    ) -> dict[str, Any]:
        """Resolve project context once before evaluating an event's rules."""
        if isinstance(project_from_vars, dict) and project_from_vars.get("path"):
            return dict(project_from_vars)

        project_info = (
            dict(project_from_vars)
            if isinstance(project_from_vars, dict)
            else {"name": "Unknown", "id": "unknown", "path": ""}
        )
        project_info.setdefault("name", "Unknown")
        project_info.setdefault("id", "unknown")
        project_info.setdefault("path", "")

        try:
            from gobby.storage.projects import LocalProjectManager
            from gobby.storage.sessions import SessionManager

            project_manager = LocalProjectManager(self.db)
            project_id = event.project_id
            session_id = event.metadata.get("_platform_session_id")
            if isinstance(session_id, str) and session_id:
                session_db = SessionManager(self.db).get(session_id)
                if session_db and session_db.project_id:
                    project_id = session_db.project_id

            if project_id:
                proj = project_manager.get(project_id)
                if proj:
                    from gobby.storage.project_checkouts import (
                        CheckoutNotFoundError,
                        require_root,
                    )
                    from gobby.storage.workspace_machine_scope import require_local_machine_id

                    checkout_path = project_info.get("path", "")
                    try:
                        machine_id = require_local_machine_id(
                            None, resource_kind="project_checkout", resource_id=proj.id
                        )
                        checkout_path = require_root(self.db, proj.id, machine_id)
                    except CheckoutNotFoundError:
                        pass
                    project_info.update(
                        {
                            "name": proj.name,
                            "id": proj.id,
                            "path": checkout_path,
                        }
                    )
        except (OSError, psycopg.Error) as e:
            logger.warning("Storage failure resolving project info for template context: %s", e)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Failed to resolve project info for template context: %s", e)

        if not project_info.get("path"):
            cwd = event.cwd or event.data.get("cwd")
            if cwd:
                project_info["path"] = cwd
        return project_info

    def _build_eval_context(
        self,
        event: HookEvent,
        variables: dict[str, Any],
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build evaluation context for condition checking."""
        raw_tool_input = event.data.get("tool_input") or event.data.get("arguments") or {}
        if not isinstance(raw_tool_input, dict):
            raw_tool_input = {}

        # For MCP call_tool, unwrap nested arguments so rule conditions
        # can reference inner tool params (commit_sha, reason, etc.) directly.
        # Preserve MCP routing fields (server_name, tool_name) so helpers like
        # is_tool_unlocked / is_discovery_tool still work after unwrapping.
        tool_name = event.data.get("tool_name", "")
        if is_gobby_call_tool(tool_name) and isinstance(raw_tool_input, dict):
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
                raw_tool_input = dict(inner_args)
            # Re-inject MCP routing fields so rule conditions can still access them
            for field in ("server_name", "tool_name"):
                if field in original_tool_input:
                    raw_tool_input[field] = original_tool_input[field]

        ctx: dict[str, Any] = {
            "variables": variables,
            "event": event,
            "tool_input": raw_tool_input,
            "source": event.source.value if event.source else None,
        }

        project_from_vars = variables.get("project")
        if isinstance(project_from_vars, dict) and project_from_vars.get("path"):
            ctx["project"] = project_from_vars
        else:
            ctx["project"] = self._resolve_project_info(event, project_from_vars)

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
        project_path = (ctx.get("project") or {}).get("path")
        event = ctx.get("event")
        event_data = event.data if isinstance(event, HookEvent) else None
        funcs = build_condition_helpers(
            task_manager=getattr(self, "_task_manager", None),
            context=ctx,
        )
        funcs["isinstance"] = isinstance
        funcs["is_tool_unlocked"] = lambda ti: is_tool_unlocked(ti, variables)
        funcs["is_discovery_tool"] = is_discovery_tool
        funcs["is_operator_tool"] = is_operator_tool
        funcs["is_plan_file"] = is_plan_file
        funcs["is_current_plan_artifact"] = (
            lambda file_path, artifact_path: is_current_plan_artifact(
                file_path, artifact_path, project_path=project_path
            )
        )
        funcs["get_touched_file_paths"] = get_touched_file_paths
        funcs["plan_write_paths_allowed"] = (
            lambda tool_input, provider, artifact_path=None, require_current_artifact=False: (
                plan_write_paths_allowed(
                    tool_input,
                    provider,
                    artifact_path,
                    require_current_artifact,
                    project_path=project_path,
                    event_data=event_data,
                )
            )
        )
        funcs["claimed_task_source_code_write"] = claimed_task_source_code_write
        funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file
        funcs["is_message_delivery_tool"] = is_message_delivery_tool
        funcs["skill_fetch_batch_directive"] = skill_fetch_batch_directive
        funcs["skill_fetch_directive"] = skill_fetch_directive
        funcs["has_pending_messages"] = self._has_pending_messages
        funcs["pending_message_count"] = self._pending_message_count
        funcs["has_active_agent_wait"] = lambda: bool(ctx.get("_has_active_agent_wait", False))
        return funcs

    def _render_template(
        self, template: str, ctx: dict[str, Any], allowed_funcs: dict[str, Callable[..., Any]]
    ) -> str:
        """Render a Jinja2 template string with eval context and helper functions."""
        if "{{" not in template:
            return template
        try:
            render_ctx = {**ctx, **allowed_funcs}
            engine = TemplateEngine(strict_undefined=False)
            return engine.render(template, render_ctx)
        except SecurityError:
            raise
        except Exception as e:
            logger.warning("Failed to render template: %s", e)
            return template

    def _has_pending_messages(self, session_id: str) -> bool:
        """Index probe: are there any undelivered messages for this session?"""
        if not is_session_uuid(session_id):
            return False
        row = self.db.fetchone(
            "SELECT 1 FROM inter_session_messages "
            "WHERE to_session = %s AND delivered_at IS NULL LIMIT 1",
            (session_id,),
        )
        return row is not None

    def _pending_message_count(self, session_id: str) -> int:
        """O(n) count of undelivered messages — only called when a block fires."""
        if not is_session_uuid(session_id):
            return 0
        row = self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM inter_session_messages "
            "WHERE to_session = %s AND delivered_at IS NULL",
            (session_id,),
        )
        return row["cnt"] if row else 0

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
        *,
        fail_closed: bool | None = None,
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
            if fail_closed is None:
                fail_closed = effect_type == "block"
            logger.error(
                "Failed to evaluate condition '%s': %s (defaulting to %s for %s effect)",
                condition,
                e,
                "True" if fail_closed else "False",
                effect_type,
            )
            return fail_closed
