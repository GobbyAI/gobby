"""Shared prompt rendering helpers for LLM feature callers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_SUMMARY_STRING_KEYS = frozenset(
    {
        "transcript_summary",
        "git_status",
        "file_changes",
        "git_diff_summary",
        "structured_context",
        "claimed_tasks",
        "session_memories",
        "first_digest_turn",
        "recent_digest_turns",
        "session_tasks",
        "todo_list",
        "previous_summary",
        "mode",
    }
)


def render_summary_prompt(prompt_template: str, context: Mapping[str, Any]) -> str:
    """Render a session-summary prompt before sending it through feature routing."""
    if not prompt_template:
        raise ValueError("prompt_template is required for session summary generation")

    formatted_context = _format_summary_context(context)
    if "{{" in prompt_template or "{%" in prompt_template or "{#" in prompt_template:
        from jinja2 import Environment
        from jinja2.exceptions import TemplateError

        env = Environment(autoescape=False)  # nosec B701 # generating text prompts
        try:
            return str(env.from_string(prompt_template).render(**formatted_context))
        except TemplateError as exc:
            raise ValueError(f"Failed to render summary prompt with Jinja template: {exc}") from exc
    try:
        return prompt_template.format(**formatted_context)
    except KeyError as exc:
        raise ValueError(
            f"Failed to render summary prompt: missing placeholder {exc.args[0]!r}"
        ) from exc
    except ValueError as exc:
        raise ValueError(f"Failed to render summary prompt with format template: {exc}") from exc


def _format_summary_context(context: Mapping[str, Any]) -> dict[str, Any]:
    formatted: dict[str, Any] = {
        key: _string_value(context.get(key, "")) for key in _SUMMARY_STRING_KEYS
    }
    formatted["last_messages"] = _prompt_value(context.get("last_messages", []))
    formatted.update(
        {
            key: _prompt_value(value)
            for key, value in context.items()
            if key not in formatted and key not in {"turns", "session"}
        }
    )
    return formatted


def _prompt_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, indent=2, default=str)
    return str(value)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
