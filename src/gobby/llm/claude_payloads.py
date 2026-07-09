"""Claude SDK payload helpers."""

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

from gobby.agents.provider_capabilities import provider_reasoning_efforts
from gobby.agents.reasoning import normalize_reasoning_effort

_SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def claude_reasoning_options(reasoning_effort: str | None) -> dict[str, Any]:
    """Return SDK kwargs for a normalized, Claude-supported reasoning effort."""
    normalized = normalize_reasoning_effort(reasoning_effort)
    if normalized is None:
        return {}
    supported_efforts = provider_reasoning_efforts("claude")
    if normalized not in supported_efforts:
        supported = ", ".join(sorted(supported_efforts))
        raise ValueError(
            f"Unsupported Claude reasoning effort '{normalized}' (expected {supported})"
        )
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


def prepare_image_data(
    image_path: str, logger: logging.Logger | None = None
) -> tuple[str, str] | str:
    """Validate and prepare image data for Claude multimodal SDK input."""
    path = Path(image_path)
    if not path.exists():
        return f"Image not found: {image_path}"

    try:
        image_data = path.read_bytes()
        image_base64 = base64.standard_b64encode(image_data).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - user-facing image read diagnostic
        if logger is not None:
            logger.error("Failed to read image %s: %s", image_path, exc)
        return f"Failed to read image: {exc}"

    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        mime_type = "image/png"

    return (image_base64, mime_type)
