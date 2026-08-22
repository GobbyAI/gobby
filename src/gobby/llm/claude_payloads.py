"""Claude SDK payload helpers."""

from typing import Any

from gobby.agents.reasoning import normalize_reasoning_effort


def claude_reasoning_options(reasoning_effort: str | None) -> dict[str, Any]:
    """Return SDK kwargs for a normalized reasoning effort."""
    normalized = normalize_reasoning_effort(reasoning_effort)
    if normalized is None or normalized == "auto":
        return {}
    return {"effort": normalized}


def _coerce_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def normalize_claude_usage(usage: Any) -> dict[str, int] | None:
    """Normalize a Claude Agent SDK usage payload into canonical token counts."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        data: dict[str, Any] = usage
    elif hasattr(usage, "model_dump"):
        data = usage.model_dump()
    else:
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
        data = {field: getattr(usage, field, None) for field in fields}

    input_tokens = _coerce_int(data.get("input_tokens"))
    output_tokens = _coerce_int(data.get("output_tokens"))
    cache_creation_input_tokens = _coerce_int(data.get("cache_creation_input_tokens"))
    cache_read_input_tokens = _coerce_int(data.get("cache_read_input_tokens"))
    prompt_tokens = _coerce_int(data.get("prompt_tokens"))
    completion_tokens = _coerce_int(data.get("completion_tokens"))
    total_tokens = _coerce_int(data.get("total_tokens"))

    if prompt_tokens is None:
        prompt_tokens = input_tokens
    if completion_tokens is None:
        completion_tokens = output_tokens
    if total_tokens is None:
        token_parts = (
            prompt_tokens,
            completion_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )
        if any(value is not None for value in token_parts):
            total_tokens = sum(value or 0 for value in token_parts)

    result: dict[str, int] = {}
    if prompt_tokens is not None:
        result["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        result["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    if input_tokens is not None:
        result["input_tokens"] = input_tokens
    if output_tokens is not None:
        result["output_tokens"] = output_tokens
    if cache_creation_input_tokens is not None:
        result["cache_creation_input_tokens"] = cache_creation_input_tokens
    if cache_read_input_tokens is not None:
        result["cache_read_input_tokens"] = cache_read_input_tokens
    return result or None


def strip_leading_preamble(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("# ") or stripped.startswith("## "):
            return "\n".join(lines[index:]).strip()
    return text.strip()
