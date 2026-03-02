"""Rendering logic for pipeline steps."""

import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)

# Env-var suffixes that indicate sensitive values (case-insensitive check).
_SENSITIVE_SUFFIXES = (
    "_SECRET",
    "_KEY",
    "_TOKEN",
    "_PASSWORD",
    "_CREDENTIAL",
    "_PRIVATE_KEY",
    "_AUTH",
    "_OAUTH",
    "_API_KEY",
)

# Specific env-var names that are always excluded.
_SENSITIVE_NAMES = frozenset(
    {
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "API_KEY",
        "AUTH_TOKEN",
        "OAUTH_TOKEN",
    }
)


def _filter_env(
    env: Mapping[str, str],
    allowed_keys: frozenset[str] | None = None,
) -> dict[str, str]:
    """Return a copy of *env* with sensitive variables removed.

    If *allowed_keys* is provided only those keys are included (explicit
    whitelist).  Otherwise a suffix/name-based blocklist is applied.
    """
    if allowed_keys is not None:
        return {k: v for k, v in env.items() if k in allowed_keys}
    return {
        k: v
        for k, v in env.items()
        if k not in _SENSITIVE_NAMES and not any(k.upper().endswith(s) for s in _SENSITIVE_SUFFIXES)
    }


class StepRenderer:
    """Handles variable substitution and type coercion for pipeline steps."""

    def __init__(
        self,
        template_engine: TemplateEngine | None = None,
        *,
        allowed_env_keys: frozenset[str] | None = None,
        strict_conditions: bool = False,
    ):
        self.template_engine = template_engine
        self.allowed_env_keys = allowed_env_keys
        self.strict_conditions = strict_conditions

    def render_step(self, step: Any, context: dict[str, Any]) -> Any:
        """Render template variables in step fields.

        Args:
            step: The step to render
            context: Context with variables for substitution

        Returns:
            Step with rendered fields
        """
        if not self.template_engine:
            return step

        # Build render context with filtered environment variables
        render_context = {
            "inputs": context.get("inputs", {}),
            "steps": context.get("steps", {}),
            "env": _filter_env(os.environ, self.allowed_env_keys),
            "session_id": context.get("session_id"),
            "parent_session_id": context.get("parent_session_id"),
        }

        # Create a copy of the step to avoid modifying the definition
        rendered_step = step.model_copy(deep=True)

        try:
            if rendered_step.exec:
                rendered_step.exec = self.render_string(rendered_step.exec, render_context)

            if rendered_step.prompt:
                rendered_step.prompt = self.render_string(rendered_step.prompt, render_context)

            if rendered_step.mcp and rendered_step.mcp.arguments:
                rendered_step.mcp.arguments = self.render_mcp_arguments(
                    rendered_step.mcp.arguments, render_context
                )

            if rendered_step.invoke_pipeline and isinstance(rendered_step.invoke_pipeline, dict):
                if "name" in rendered_step.invoke_pipeline and isinstance(
                    rendered_step.invoke_pipeline["name"], str
                ):
                    rendered_step.invoke_pipeline["name"] = self.render_string(
                        rendered_step.invoke_pipeline["name"], render_context
                    )
                if "arguments" in rendered_step.invoke_pipeline and isinstance(
                    rendered_step.invoke_pipeline["arguments"], dict
                ):
                    rendered_step.invoke_pipeline["arguments"] = self.render_mcp_arguments(
                        rendered_step.invoke_pipeline["arguments"], render_context
                    )

        except Exception as e:
            raise ValueError(f"Failed to render step {step.id}: {e}") from e

        return rendered_step

    def render_string(self, s: str, context: dict[str, Any]) -> str:
        """Render a string with template variables."""
        if not s or not self.template_engine:
            return s

        # Replace ${{ ... }} with {{ ... }} for Jinja2
        # Use dotall to allow multi-line expressions
        jinja_template = re.sub(r"\$\{\{(.*?)\}\}", r"{{\1}}", s, flags=re.DOTALL)

        return self.template_engine.render(jinja_template, context)

    def _coerce_value(self, value: Any) -> Any:
        """Auto-coerce rendered string values to native types.

        After template rendering, values like "${{ inputs.timeout }}" become "600" (string).
        MCP tools expect native types, so coerce: "600" → 600, "true" → True, etc.
        """
        if not isinstance(value, str):
            return value
        # Empty string (e.g. Jinja2 rendered None → "")
        if value == "":
            return None
        # Boolean
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        # Null
        if value.lower() in ("null", "none"):
            return None
        # Integer
        try:
            return int(value)
        except ValueError:
            pass
        # Float
        try:
            return float(value)
        except ValueError:
            pass
        return value

    def render_mcp_arguments(self, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Render template variables in MCP arguments and coerce types."""
        rendered: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str):
                rendered_val = self.render_string(value, context)
                rendered[key] = self._coerce_value(rendered_val)
            elif isinstance(value, dict):
                rendered[key] = self.render_mcp_arguments(value, context)
            elif isinstance(value, list):
                rendered[key] = self._render_list(value, context)
            else:
                rendered[key] = value
        return rendered

    def _render_list(self, items: list[Any], context: dict[str, Any]) -> list[Any]:
        """Render template variables in a list, handling nested dicts and lists."""
        result = []
        for v in items:
            if isinstance(v, str):
                result.append(self._coerce_value(self.render_string(v, context)))
            elif isinstance(v, dict):
                result.append(self.render_mcp_arguments(v, context))
            elif isinstance(v, list):
                result.append(self._render_list(v, context))
            else:
                result.append(v)
        return result

    def resolve_reference(self, ref: str, context: dict[str, Any]) -> Any:
        """Resolve a $step.output reference from context.

        Args:
            ref: Reference string like "$step1.output" or "$step1.output.field"
            context: Execution context

        Returns:
            The resolved value
        """
        # Parse reference: $step_id.output[.field]
        match = re.match(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\.output(?:\.(.+))?", ref)
        if not match:
            return ref

        step_id = match.group(1)
        field_path = match.group(2)

        # Get step output from context
        step_data = context.get("steps", {}).get(step_id, {})
        output = step_data.get("output")

        if field_path and isinstance(output, dict):
            # Navigate nested field path
            for part in field_path.split("."):
                if isinstance(output, dict):
                    output = output.get(part)
                else:
                    break

        return output

    def should_run_step(self, step: Any, context: dict[str, Any]) -> bool:
        """Check if a step should run based on its condition."""
        # No condition means always run
        if not step.condition:
            return True

        # Strip ${{ }} wrapper (pipeline template syntax)
        condition = step.condition.strip()
        m = re.match(r"^\$\{\{\s*(.*?)\s*\}\}$", condition, re.DOTALL)
        if m:
            condition = m.group(1).strip()

        try:
            # Evaluate the condition using safe AST-based evaluator
            eval_context = {
                "inputs": context.get("inputs", {}),
                "steps": context.get("steps", {}),
                "session_id": context.get("session_id"),
                "parent_session_id": context.get("parent_session_id"),
            }
            # Allow common helper functions for conditions
            allowed_funcs: dict[str, Any] = {
                "len": len,
                "bool": bool,
                "str": str,
                "int": int,
            }
            evaluator = SafeExpressionEvaluator(eval_context, allowed_funcs)
            return evaluator.evaluate(condition)
        except ValueError as e:
            if self.strict_conditions:
                raise ValueError(f"Condition evaluation failed for step {step.id}: {e}") from e
            logger.warning(f"Condition evaluation failed for step {step.id}: {e}")
            # Default to running the step if condition evaluation fails
            return True
