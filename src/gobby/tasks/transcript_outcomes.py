"""Classify bounded shell output and definitive validation outcomes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from gobby.config.shell_lexing import ParsedShellCommand
from gobby.sessions.transcript_tool_metadata import extract_result_metadata
from gobby.tasks.command_equivalence import parse_validation_shell

EvidenceOutcome = Literal["success", "failure", "unknown"]

_EXIT_CODE_KEYS = ("exit_code", "exitCode")
_SUCCESS_STATUSES = {"completed", "ok", "passed", "success", "succeeded"}
_FAILURE_STATUSES = {"error", "failed", "failure"}
_OUTPUT_CHAR_LIMIT = 16_000
_ENV_ASSIGNMENT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# The rtk hook rewrites shell commands as `rtk <cmd>` or `uv run rtk <cmd>`;
# rtk filters output and propagates the wrapped command's exit code (#21766).
_RTK_WRAPPER_PREFIX = re.compile(r"^(uv\s+run\s+)?rtk\s+")
_SHELL_COMMAND_WRAPPERS = {"bash", "fish", "sh", "zsh"}
_NODE_WRAPPERS = {"node", "nodejs"}

_RUNNER_FAILURE_PATTERNS = (
    re.compile(r"\b[1-9]\d*[^\S\n]+(?:failed|failures?|errors?)\b", re.IGNORECASE),
    re.compile(
        r"(?m)^\s*(?:FAILED\s+\S|ERROR\s+\S+::|---\s+FAIL:|FAIL\s+\S|test result:\s*FAILED\b)"
    ),
    re.compile(r"(?m)^\s*Failing new (?:errors|issues) >= \w+: [1-9]\d*\b"),
)

_HOOK_BLOCKING_ATTACHMENT = "hook_blocking_error"
_TOOL_DENIAL_KIND_KEYS = ("toolDenialKind", "tool_denial_kind")
_UNEXECUTED_TOOL_PATTERNS = (
    re.compile(r"the user doesn't want to proceed with this tool use", re.IGNORECASE),
    re.compile(r"the tool use was rejected", re.IGNORECASE),
    re.compile(r"rule enforced by gobby\b", re.IGNORECASE),
    re.compile(r"hook denied:", re.IGNORECASE),
    re.compile(r"hook blocked this tool call", re.IGNORECASE),
    re.compile(r"permission to use \S+ (?:has been )?denied", re.IGNORECASE),
)

_TYPE_CHECK_FAILURE_PATTERNS = (
    re.compile(r"(?m)^Found [1-9]\d* errors? in [1-9]\d* files?\b"),
    re.compile(r"(?m)^Failing new errors >= \w+: [1-9]\d*\b"),
)
_TEST_FAILURE_PATTERNS = (
    re.compile(r"\b[1-9]\d*[^\S\n]+(?:failed|failures?)\b", re.IGNORECASE),
    re.compile(
        r"(?m)^\s*(?:FAILED\s+\S|ERROR\s+\S+::|---\s+FAIL:|FAIL\s+\S|test result:\s*FAILED\b)"
    ),
    re.compile(r"(?m)^=+ [1-9]\d* errors? in \d"),
    re.compile(r"(?m)^Failing new issues >= \w+: [1-9]\d*\b"),
)


@dataclass(frozen=True, slots=True)
class ValidationCommandEquivalence:
    """Criteria-matching view of one verbatim validation command."""

    core_command: str | None
    wrapped: bool
    wrapper_reason: str | None


def classify_validation_command_equivalence(command: str) -> ValidationCommandEquivalence:
    """Strip approved prefixes and reject wrappers that can obscure exit status."""
    core_command = _strip_exit_preserving_prefixes(command)
    reason = _wrapper_reason(core_command)
    if reason is not None:
        return ValidationCommandEquivalence(None, True, reason)
    return ValidationCommandEquivalence(core_command, False, None)


_VALIDATION_RUNNER_PREFIXES = (
    ("uv", "run"),
    ("npm", "exec"),
    ("pnpm", "exec"),
    ("python", "-m"),
    ("python3", "-m"),
    ("npx",),
    ("pnpm",),
    ("yarn",),
)
# Executables whose bare name is already a validation run.
_VALIDATION_EXECUTABLES = frozenset({"pytest", "mypy", "vitest", "jest"})
# Executables that run validation only under specific subcommands, in order.
_VALIDATION_SUBCOMMANDS: dict[str, tuple[frozenset[str], ...]] = {
    "cargo": (frozenset({"test", "nextest", "clippy", "fmt"}),),
    "ruff": (frozenset({"check", "format"}),),
    "gobby": (frozenset({"test-types", "test-quality"}), frozenset({"audit"})),
}


def wrapped_validation_command(command: object) -> str | None:
    """Name the wrapper that would void close-gate credit for a validation run.

    Returns ``None`` when the call runs no recognized validation executable or
    when the run is credited as it stands. A command the validation parser
    cannot read (subshells, expansions) exposes no recognizable segment and so
    reports nothing rather than guessing.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    core = _strip_exit_preserving_prefixes(command)
    segments = parse_validation_shell(core).segments
    if not any(_segment_runs_validation(segment) for segment in segments):
        return None
    return _wrapper_reason(core)


def _segment_runs_validation(segment: tuple[str, ...]) -> bool:
    tokens = list(segment)
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_PREFIX.match(tokens[index]):
        index += 1
    consumed = True
    while consumed:
        consumed = False
        for prefix in _VALIDATION_RUNNER_PREFIXES:
            if tuple(tokens[index : index + len(prefix)]) == prefix:
                index += len(prefix)
                consumed = True
                break
    if index >= len(tokens):
        return False
    executable = os.path.basename(tokens[index])
    arguments = tokens[index + 1 :]
    if executable == "cargo" and arguments and arguments[0].startswith("+"):
        arguments = arguments[1:]
    if executable in _VALIDATION_EXECUTABLES:
        return True
    subcommands = _VALIDATION_SUBCOMMANDS.get(executable)
    if subcommands is None or len(arguments) < len(subcommands):
        return False
    return all(
        argument in allowed for argument, allowed in zip(arguments, subcommands, strict=False)
    )


def _strip_exit_preserving_prefixes(command: str) -> str:
    cursor = _skip_whitespace(command, 0)
    while (next_cursor := _consume_cd_prefix(command, cursor)) is not None:
        cursor = _skip_whitespace(command, next_cursor)
    while _ENV_ASSIGNMENT_PREFIX.match(command, cursor):
        word_end = _shell_word_end(command, cursor)
        if word_end is None or word_end >= len(command) or not command[word_end].isspace():
            break
        cursor = _skip_whitespace(command, word_end)
    return _RTK_WRAPPER_PREFIX.sub(r"\1", command[cursor:].strip(), count=1)


def _consume_cd_prefix(command: str, cursor: int) -> int | None:
    if not command.startswith("cd", cursor):
        return None
    name_end = cursor + 2
    if name_end >= len(command) or not command[name_end].isspace():
        return None
    path_start = _skip_whitespace(command, name_end)
    path_end = _shell_word_end(command, path_start)
    if path_end is None:
        return None
    operator_start = path_end
    while operator_start < len(command) and command[operator_start] in " \t\r":
        operator_start += 1
    if operator_start < len(command) and command[operator_start] == "\n":
        return operator_start + 1
    if not command.startswith("&&", operator_start):
        return None
    return operator_start + 2


def _shell_word_end(command: str, start: int) -> int | None:
    cursor = start
    quote: str | None = None
    while cursor < len(command):
        char = command[cursor]
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"' and cursor + 1 < len(command):
                cursor += 1
        elif char in {"'", '"'}:
            quote = char
        elif char == "\\" and cursor + 1 < len(command):
            cursor += 1
        elif char.isspace() or char in ";&|()":
            break
        cursor += 1
    if cursor == start or quote is not None:
        return None
    return cursor


def _skip_whitespace(command: str, cursor: int) -> int:
    while cursor < len(command) and command[cursor].isspace():
        cursor += 1
    return cursor


def _wrapper_reason(command: str) -> str | None:
    stripped = command.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        return "subshell wrapper"
    if stripped.startswith("js_repl("):
        return "js_repl wrapper"
    parsed = parse_validation_shell(stripped)
    if any(operator in {"|", "|&"} for operator in parsed.operators):
        return "pipeline"
    trailing_output = _trailing_output_command(parsed)
    if trailing_output is not None:
        return f"trailing {trailing_output}"
    if "||" in parsed.operators:
        return "fallback"
    if "&" in parsed.operators:
        return "backgrounding"
    if any(operator in {";", "\n"} for operator in parsed.operators):
        return "command sequence"
    if not parsed.segments or len(parsed.segments) != len(parsed.operators) + 1:
        return "unsupported shell structure"

    for segment in parsed.segments:
        reason = _executable_wrapper_reason(ParsedShellCommand((segment,), ()))
        if reason is not None:
            return reason
    return None


def _executable_wrapper_reason(parsed: ParsedShellCommand) -> str | None:
    executable, arguments = _first_executable(parsed)
    if executable == "nohup":
        return "nohup wrapper"
    if executable in _SHELL_COMMAND_WRAPPERS and any(
        argument in {"-c", "-lc"} for argument in arguments
    ):
        return "subshell wrapper"
    if executable == "js_repl":
        return "js_repl wrapper"
    if executable in _NODE_WRAPPERS:
        return "node wrapper"
    return None


def _trailing_output_command(parsed: ParsedShellCommand) -> str | None:
    if not parsed.operators or parsed.operators[-1] not in {";", "&&"}:
        return None
    if not parsed.segments or not parsed.segments[-1]:
        return None
    segment = parsed.segments[-1]
    executable_index = 0
    while executable_index < len(segment) and _ENV_ASSIGNMENT_PREFIX.match(
        segment[executable_index]
    ):
        executable_index += 1
    if executable_index >= len(segment):
        return None
    executable = os.path.basename(segment[executable_index])
    return executable if executable in {"echo", "printf"} else None


def _first_executable(parsed: ParsedShellCommand) -> tuple[str, tuple[str, ...]]:
    if not parsed.segments or not parsed.segments[0]:
        return "", ()
    tokens = parsed.segments[0]
    executable_index = 0
    while executable_index < len(tokens) and _ENV_ASSIGNMENT_PREFIX.match(tokens[executable_index]):
        executable_index += 1
    if executable_index >= len(tokens):
        return "", ()
    executable = os.path.basename(tokens[executable_index])
    arguments = tokens[executable_index + 1 :]
    if executable == "uv" and len(arguments) >= 2 and arguments[0] == "run":
        executable = os.path.basename(arguments[1])
        arguments = arguments[2:]
    return executable, arguments


def is_unexecuted_tool_result(result: Any) -> bool:
    """Return whether a tool result shows the call never executed.

    User rejections, permission denials, and hook blocks are tool-layer errors
    with no process exit. They must not become validation runs. A command that
    actually ran still has an exit code, or output that is not this class.
    """
    if _find_exit_code(result) is not None:
        return False
    for value in _walk_values(result):
        if not isinstance(value, dict):
            continue
        for key in _TOOL_DENIAL_KIND_KEYS:
            kind = value.get(key)
            if isinstance(kind, str) and kind.strip():
                return True
        attachment = value.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == _HOOK_BLOCKING_ATTACHMENT:
            return True
        if value.get("type") == _HOOK_BLOCKING_ATTACHMENT:
            return True
    output, _truncated = extract_output(result)
    if not output:
        return False
    return any(pattern.search(output) for pattern in _UNEXECUTED_TOOL_PATTERNS)


def extract_output(result: Any) -> tuple[str | None, bool]:
    """Extract bounded command output needed to classify validation failures."""
    if isinstance(result, dict) and "outcome_provenance" in result:
        result = {key: value for key, value in result.items() if key != "outcome_provenance"}
    parts: list[str] = []
    seen: set[str] = set()
    for value in _walk_values(result):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    if not parts:
        return None, False
    output = "\n".join(parts)
    if len(output) <= _OUTPUT_CHAR_LIMIT:
        return output, False
    half = (_OUTPUT_CHAR_LIMIT - len("\n...[output truncated]...\n")) // 2
    return (
        f"{output[:half]}\n...[output truncated]...\n{output[-half:]}",
        True,
    )


def extract_outcome(
    result: Any,
    output: str | None = None,
    *,
    aggregate_status_is_trustworthy: bool = True,
) -> tuple[EvidenceOutcome, int | None, str | None]:
    """Classify one shell result as a validation pass, failure, or unknown."""
    exit_code = _find_exit_code(result)
    if _runner_reported_failures(output) and (
        not aggregate_status_is_trustworthy or exit_code is None
    ):
        return "failure", exit_code, None

    if exit_code is not None:
        return ("success" if exit_code == 0 else "failure"), exit_code, None

    values = list(_walk_values(result))
    for value in values:
        if not isinstance(value, dict):
            continue
        success = value.get("success")
        if isinstance(success, bool):
            return ("success" if success else "failure"), None, None
        status = value.get("status")
        if isinstance(status, str):
            normalized = status.strip().casefold()
            if normalized in _SUCCESS_STATUSES:
                return "success", None, None
            if normalized in _FAILURE_STATUSES:
                return "failure", None, None
        is_error = value.get("is_error")
        if isinstance(is_error, bool):
            return ("failure" if is_error else "success"), None, None
        error = value.get("error")
        if error not in (None, "", False, []):
            return "failure", None, None

    unknown_reason = None
    for value in values:
        if isinstance(value, dict):
            reason = value.get("unknown_reason")
            if isinstance(reason, str) and reason:
                unknown_reason = reason
                break
    return "unknown", None, unknown_reason or "missing definitive provider outcome"


def infer_failure_categories(output: str | None) -> frozenset[str]:
    """Return validation categories identified by runner-specific failure output."""
    if not output:
        return frozenset()
    categories: set[str] = set()
    if any(pattern.search(output) for pattern in _TYPE_CHECK_FAILURE_PATTERNS):
        categories.add("type_check")
    if any(pattern.search(output) for pattern in _TEST_FAILURE_PATTERNS):
        categories.add("test")
    return frozenset(categories)


def _runner_reported_failures(output: str | None) -> bool:
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_FAILURE_PATTERNS)


def _find_exit_code(result: Any) -> int | None:
    for value in _walk_values(result):
        if not isinstance(value, dict):
            continue
        metadata = extract_result_metadata("bash", value)
        candidates = [metadata.get("exit_code"), *(value.get(key) for key in _EXIT_CODE_KEYS)]
        for candidate in candidates:
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    return None


def _walk_values(value: Any, *, depth: int = 0) -> Iterable[Any]:
    if depth > 8:
        return
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_values(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_values(nested, depth=depth + 1)
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk_values(decoded, depth=depth + 1)
