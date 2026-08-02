"""Claude Agent SDK execution policy."""

import asyncio
import logging
import random
import re
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError

from gobby.llm.base import LLMProviderCancellation
from gobby.llm.claude_errors import (
    ClaudeSDKProviderFailure,
    classify_result_message,
    is_connectivity_error,
)
from gobby.shutdown_intent import read_active_shutdown_intent

_HEADLESS_SETTINGS = Path.home() / ".gobby" / "settings" / "headless.json"
_STDERR_MAX_LINES = 200
_PERMANENT_ERROR_PATTERN = re.compile(
    r"\b(?:401|403|404)\b"
    r"|\binvalid[_ ]api[_ ]key\b"
    r"|\bauthentication\b"
    r"|\bunauthorized\b"
    r"|\binvalid[_ ]request\b"
    r"|\bpermission denied\b"
    r"|\bnot_found\b",
    re.IGNORECASE,
)


class ClaudeSDKShutdownCancellation(LLMProviderCancellation):
    """Claude SDK process was cancelled during shutdown."""


def is_transient_error(error: Exception) -> bool:
    """Classify whether an error is worth retrying."""
    if isinstance(error, LLMProviderCancellation):
        return False
    if isinstance(error, CLINotFoundError):
        return False
    if is_sdk_sigterm_shutdown(error):
        return False
    if isinstance(error, ClaudeSDKProviderFailure):
        return False
    if _is_error_result_success(error):
        return False

    return _PERMANENT_ERROR_PATTERN.search(str(error)) is None


def _is_error_result_success(error: BaseException) -> bool:
    """Return whether the SDK surfaced its known error-result-success shape."""
    return "claude code returned an error result: success" in str(error).lower()


def raise_for_error_result(
    message: Any, operation: str, *, rate_limit_info: Any | None = None
) -> None:
    """Raise a classified failure before the SDK wraps an error ResultMessage."""
    if getattr(message, "is_error", False):
        raise classify_result_message(message, operation, rate_limit_info=rate_limit_info)


def is_max_turns_error(error: BaseException) -> bool:
    """Return whether the SDK raised because it hit ``max_turns``."""
    for current in _iter_exception_tree(error):
        if "maximum number of turns" in str(current).lower():
            return True
    return False


def is_sdk_sigterm_shutdown(error: BaseException) -> bool:
    """Return whether the Claude SDK/process was terminated by SIGTERM."""
    return extract_exit_code(error) == 143


def _is_planned_shutdown_execution_error(error: BaseException) -> bool:
    """Return whether child reaping interrupted an SDK query during planned shutdown."""
    if not isinstance(error, ClaudeSDKProviderFailure) or error.subtype != "error_during_execution":
        return False
    shutdown_record = read_active_shutdown_intent()
    return (
        shutdown_record is not None and not shutdown_record.stale and shutdown_record.error is None
    )


def _iter_exception_tree(error: BaseException) -> Iterator[BaseException]:
    """Walk exception, causes, contexts, and exception-group children."""
    stack: list[BaseException] = [error]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        yield current
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
        children = getattr(current, "exceptions", None)
        if isinstance(children, tuple):
            stack.extend(child for child in children if isinstance(child, BaseException))


def _extract_exit_code_from_message(message: str) -> int | None:
    """Parse common SDK/process messages like 'exit code 143'."""
    normalized = message.lower().replace("_", " ").replace("=", " ").replace(":", " ")
    parts = normalized.split()
    for index, part in enumerate(parts[:-2]):
        if part.strip("([{") == "exit" and parts[index + 1] == "code":
            candidate = parts[index + 2].strip(".,;])}")
            try:
                return int(candidate)
            except ValueError:
                return None
    return None


def extract_exit_code(error: BaseException) -> int | None:
    """Walk exception tree to find ProcessError exit code."""
    for current in _iter_exception_tree(error):
        exit_code = getattr(current, "exit_code", None)
        if exit_code is not None:
            try:
                return int(exit_code)
            except (TypeError, ValueError):
                pass
        parsed = _extract_exit_code_from_message(str(current))
        if parsed is not None:
            return parsed
    return None


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    delay: float = 1.0,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """Execute an async operation with retry logic and error classification."""
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    for attempt in range(max_retries):
        try:
            return await operation()
        except Exception as error:
            if not is_transient_error(error):
                raise
            if attempt < max_retries - 1:
                if on_retry:
                    on_retry(attempt, error)
                backoff = delay * (2**attempt) + random.uniform(0, delay * 0.5)  # nosec B311
                await asyncio.sleep(backoff)
            else:
                raise
    raise AssertionError("retry_async exhausted without returning or raising")


async def execute_sdk_query[T](
    operation: str,
    query_fn: Callable[[], Awaitable[T]],
    options: ClaudeAgentOptions,
    logger: logging.Logger,
    *,
    max_retries: int = 1,
    retry_delay: float = 2.0,
) -> T:
    """Execute an SDK query with stderr capture, retry, drain, and diagnostics."""
    if not options.settings:
        options.settings = str(_HEADLESS_SETTINGS)
    if not options.setting_sources:
        options.setting_sources = []

    stderr_lines: deque[str] = deque(maxlen=_STDERR_MAX_LINES)
    options.stderr = lambda line: stderr_lines.append(line)

    def _on_retry(attempt: int, error: Exception) -> None:
        if stderr_lines:
            logger.warning(
                "%s failed (attempt %d), retrying: %s stderr=%s",
                operation,
                attempt + 1,
                error,
                list(stderr_lines),
            )
        else:
            logger.warning(
                "%s failed (attempt %d), retrying: %s",
                operation,
                attempt + 1,
                error,
            )
        stderr_lines.clear()

    def _shutdown_cancellation(error: BaseException) -> ClaudeSDKShutdownCancellation:
        exit_code = extract_exit_code(error) or 143
        stderr_text = "\n".join(stderr_lines)
        message = (
            f"{operation} cancelled: Claude SDK process terminated "
            f"[exit_code={exit_code}]" + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else "")
        )
        logger.info(
            "%s cancelled: Claude SDK process terminated [exit_code=%s]%s",
            operation,
            exit_code,
            f"\nCLI stderr:\n{stderr_text}" if stderr_text else "",
        )
        return ClaudeSDKShutdownCancellation(message)

    try:
        return await retry_async(
            query_fn, max_retries=max_retries, delay=retry_delay, on_retry=_on_retry
        )
    except Exception as error:
        if is_sdk_sigterm_shutdown(error) or _is_planned_shutdown_execution_error(error):
            raise _shutdown_cancellation(error) from error

        if isinstance(error, ClaudeSDKProviderFailure):
            if is_connectivity_error(error):
                logger.debug("%s", error)
            else:
                logger.warning("%s", error)
            raise

        if _is_error_result_success(error):
            exit_code = extract_exit_code(error)
            stderr_text = "\n".join(stderr_lines)
            message = (
                f"{operation} provider degraded: Claude SDK returned "
                "error-result-success"
                + (f" [exit_code={exit_code}]" if exit_code else "")
                + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else "")
            )
            wrapped = ClaudeSDKProviderFailure(message, classification="error_result")
            if is_connectivity_error(wrapped) or is_connectivity_error(error):
                logger.debug("%s", message)
            else:
                logger.warning("%s", message)
            raise wrapped from error

        await asyncio.sleep(0.2)
        exit_code = extract_exit_code(error)
        stderr_text = "\n".join(stderr_lines)
        stderr_suffix = f"\nCLI stderr:\n{stderr_text}" if stderr_text else " (no stderr captured)"
        logger.exception(
            "%s failed: %s%s%s",
            operation,
            error,
            f" [exit_code={exit_code}]" if exit_code else "",
            stderr_suffix,
        )
        raise RuntimeError(
            f"{operation} failed: {error}"
            + (f"\nCLI stderr:\n{stderr_text}" if stderr_text else "")
        ) from error
