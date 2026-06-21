"""Output rendering helpers for PipelineExecutor."""

from __future__ import annotations

import re
from typing import Any


def _coerce_rendered_value(value: Any) -> Any:
    """Coerce Jinja2-rendered string values back to native Python types.

    Jinja2 renders everything as strings, so boolean False becomes "False"
    which is truthy. This coerces known literals back to their native types.
    """
    if not isinstance(value, str):
        return value
    lower = value.strip().lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "none":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class PipelineExecutorOutputMixin:
    """Pipeline output and reference resolution helpers."""

    renderer: Any

    def _build_outputs(self, pipeline: Any, context: dict[str, Any]) -> dict[str, Any]:
        """Build pipeline outputs from context.

        Args:
            pipeline: The pipeline definition
            context: Final execution context

        Returns:
            Dict of output name -> value
        """
        outputs: dict[str, Any] = {}
        # Build render context once for Jinja2 expressions
        render_ctx = (
            self.renderer.build_render_context(context) if self.renderer.template_engine else {}
        )

        for name, expr in pipeline.outputs.items():
            if isinstance(expr, str) and "${{" in expr:
                # Pure expression: use SafeExpressionEvaluator (has len, bool, etc.)
                m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expr.strip(), re.DOTALL)
                if m:
                    resolved = self.renderer._resolve_expression(m.group(1), render_ctx)
                    outputs[name] = (
                        _coerce_rendered_value(resolved) if isinstance(resolved, str) else resolved
                    )
                else:
                    # Mixed string with embedded ${{ }}: use Jinja2
                    rendered = self.renderer.render_string(expr, render_ctx)
                    outputs[name] = _coerce_rendered_value(rendered)
            elif isinstance(expr, str) and expr.startswith("$"):
                # Resolve $step.output reference
                value = self._resolve_reference(expr, context)
                outputs[name] = value
            else:
                outputs[name] = expr

        return outputs

    def _resolve_reference(self, ref: str, context: dict[str, Any]) -> Any:
        """Resolve a $step.output reference from context."""
        return self.renderer.resolve_reference(ref, context)
