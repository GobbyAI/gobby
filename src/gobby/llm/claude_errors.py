"""Claude SDK failure classification helpers."""

from __future__ import annotations

import re
import time
from typing import Any


class ClaudeSDKProviderFailure(RuntimeError):
    """Typed failure for known Claude SDK/provider degradation paths."""

    def __init__(
        self,
        message: str,
        *,
        classification: str = "provider_degraded",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.retry_after = retry_after


class ClaudeSDKMaxTurns(ClaudeSDKProviderFailure):
    """Bounded agentic completion caused by the configured turn limit."""

    def __init__(self, message: str) -> None:
        super().__init__(message, classification="max_turns")


class ClaudeSDKRateLimited(ClaudeSDKProviderFailure):
    """The Claude subscription or API reported a usage/rate limit."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        reset_at: float | None = None,
    ) -> None:
        super().__init__(message, classification="rate_limited", retry_after=retry_after)
        self.reset_at = reset_at


_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "usage limit reached",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded_error",
    "overloaded",
)
_USAGE_LIMIT_RESET_RE = re.compile(r"\|\s*(\d{9,13})\s*$")


def _parse_usage_limit_reset(text: str, *, now: float) -> tuple[float | None, float | None]:
    """Return ``(reset_epoch_seconds, retry_after_seconds)`` from a usage-limit body."""
    match = _USAGE_LIMIT_RESET_RE.search(text)
    if not match:
        return None, None
    raw = int(match.group(1))
    reset_epoch = raw / 1000.0 if raw > 1_000_000_000_000 else float(raw)
    retry_after = reset_epoch - now
    return reset_epoch, retry_after if retry_after > 0.0 else None


def classify_result_message(
    message: Any,
    operation: str,
    *,
    now: float | None = None,
    rate_limit_info: Any | None = None,
) -> ClaudeSDKProviderFailure:
    """Build a typed, classified failure from an ``is_error`` ResultMessage."""
    now = time.time() if now is None else now
    result_text = (getattr(message, "result", None) or "").strip()
    subtype = getattr(message, "subtype", None) or "unknown"
    api_status = getattr(message, "api_error_status", None)
    lowered = result_text.lower()

    rl_status = getattr(rate_limit_info, "status", None)
    rl_resets_at = getattr(rate_limit_info, "resets_at", None)
    rl_type = getattr(rate_limit_info, "rate_limit_type", None)

    is_rate_limit = (
        rl_status == "rejected"
        or api_status == 429
        or any(marker in lowered for marker in _RATE_LIMIT_MARKERS)
    )
    detail = result_text or f"subtype={subtype}"
    if subtype == "error_max_turns":
        return ClaudeSDKMaxTurns(f"{operation} reached max_turns: {detail}")

    if is_rate_limit:
        reset_at: float | None = float(rl_resets_at) if rl_resets_at else None
        retry_after: float | None = None
        if reset_at is not None:
            remaining = reset_at - now
            retry_after = remaining if remaining > 0.0 else None
        else:
            reset_at, retry_after = _parse_usage_limit_reset(result_text, now=now)
        parts = [f"{operation} provider rate-limited: {detail}"]
        if rl_type:
            parts.append(f"[window={rl_type}]")
        if api_status:
            parts.append(f"[api_error_status={api_status}]")
        if retry_after:
            parts.append(f"[retry_after={retry_after:.0f}s]")
        return ClaudeSDKRateLimited(" ".join(parts), retry_after=retry_after, reset_at=reset_at)

    parts = [
        f"{operation} provider degraded: Claude SDK returned error result "
        f"(subtype={subtype}): {detail}"
    ]
    if api_status:
        parts.append(f"[api_error_status={api_status}]")
    return ClaudeSDKProviderFailure(" ".join(parts), classification="error_result")
