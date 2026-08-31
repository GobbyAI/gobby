"""GitHub MCP call loop extracted from GitHubIssueTriageService."""

from __future__ import annotations

import inspect
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from gobby.github_triage.delivery import TransientDeliveryError
from gobby.github_triage.service import GitHubMCPError

logger = logging.getLogger(__name__)


class _RateLimitMetadata(TypedDict):
    status_code: int | None
    retry_after_seconds: float | None
    rate_limit_remaining: int | None
    rate_limit_reset: float | None


async def github_call(
    manager: Any,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    required: bool = True,
    time_func: Callable[[], float],
    sleep_func: Callable[[float], Awaitable[None]],
    max_rate_limit_delay: float,
) -> Any:
    """Call a GitHub MCP tool by resolved server id with one rate-limit retry."""
    if manager is None:
        if required:
            raise RuntimeError("GitHub MCP manager is not configured")
        return None
    for attempt in range(2):
        try:
            if hasattr(manager, "call_tool"):
                result = manager.call_tool(server_id, tool_name=tool_name, arguments=arguments)
                if inspect.isawaitable(result):
                    result = await result
            else:
                session = await manager.get_client_session(server_id)
                result = await session.call_tool(tool_name, arguments)
            return parse_mcp_result(result, tool_name=tool_name)
        except GitHubMCPError as exc:
            if attempt or not exc.is_rate_limited:
                raise
            delay = exc.retry_delay(now=time_func(), maximum=max_rate_limit_delay)
            logger.warning(
                "GitHub MCP tool %s was rate limited; retrying once after %.3fs",
                tool_name,
                delay,
            )
            await sleep_func(delay)
        except (TimeoutError, ConnectionError):
            raise TransientDeliveryError("GitHub MCP transport failed") from None
        except Exception:
            raise GitHubMCPError(tool_name=tool_name) from None
    raise AssertionError("GitHub MCP retry loop exhausted unexpectedly")


def parse_mcp_result(result: Any, *, tool_name: str | None = None) -> Any:
    payload = _mcp_result_payload(result)
    if bool(_mcp_field(result, "isError", "is_error")):
        metadata = _safe_rate_limit_metadata(payload)
        raise GitHubMCPError(
            tool_name=tool_name,
            status_code=metadata["status_code"],
            retry_after_seconds=metadata["retry_after_seconds"],
            rate_limit_remaining=metadata["rate_limit_remaining"],
            rate_limit_reset=metadata["rate_limit_reset"],
        )
    return payload


def _mcp_result_payload(result: Any) -> Any:
    structured = _mcp_field(result, "structuredContent", "structured_content")
    if structured is not None:
        return structured
    content = _mcp_field(result, "content")
    if isinstance(content, list):
        for item in content:
            text = _mcp_field(item, "text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


def _mcp_field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _safe_rate_limit_metadata(payload: Any) -> _RateLimitMetadata:
    values: dict[str, Any] = {}
    for mapping in _nested_mappings(payload):
        for key, value in mapping.items():
            normalized = str(key).strip().lower().replace("_", "-")
            values.setdefault(normalized, value)

    status = _first_number(values, "status", "status-code", "statuscode", "http-status")
    retry_after = _first_number(values, "retry-after", "retryafter")
    remaining = _first_number(
        values,
        "x-ratelimit-remaining",
        "x-rate-limit-remaining",
        "rate-limit-remaining",
    )
    reset = _first_number(
        values,
        "x-ratelimit-reset",
        "x-rate-limit-reset",
        "rate-limit-reset",
    )
    status_code = (
        int(status) if status is not None and status.is_integer() and 100 <= status <= 599 else None
    )
    rate_limit_remaining = (
        int(remaining) if remaining is not None and remaining.is_integer() else None
    )
    return {
        "status_code": status_code,
        "retry_after_seconds": retry_after,
        "rate_limit_remaining": rate_limit_remaining,
        "rate_limit_reset": reset,
    }


def _nested_mappings(value: Any, *, depth: int = 0) -> list[dict[Any, Any]]:
    if depth > 4:
        return []
    if isinstance(value, dict):
        mappings = [value]
        for nested in value.values():
            mappings.extend(_nested_mappings(nested, depth=depth + 1))
        return mappings
    if isinstance(value, list):
        mappings = []
        for nested in value:
            mappings.extend(_nested_mappings(nested, depth=depth + 1))
        return mappings
    return []


def _first_number(values: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    return None
