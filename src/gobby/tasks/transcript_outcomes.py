"""Classify bounded shell output and definitive validation outcomes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from gobby.config.shell_lexing import ParsedShellCommand
from gobby.config.validation_detection import (
    is_validation_command,
    normalize_validation_evidence_command,
    resolve_validation_detection_config,
)
from gobby.config.validation_outcomes import runner_reported_failures
from gobby.sessions.transcript_tool_metadata import extract_result_metadata
from gobby.tasks.command_equivalence import parse_validation_shell

EvidenceOutcome = Literal["success", "failure", "unknown"]

_EXIT_CODE_KEYS = ("exit_code", "exitCode")
_ERROR_EXIT_HEADER = re.compile(r"Exit code (-?\d+)")
_SUCCESS_STATUSES = {"completed", "ok", "passed", "success", "succeeded"}
_FAILURE_STATUSES = {"error", "failed", "failure"}
_OUTPUT_CHAR_LIMIT = 16_000
_ENV_ASSIGNMENT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_COMMAND_WRAPPERS = {"bash", "fish", "sh", "zsh"}
_NODE_WRAPPERS = {"node", "nodejs"}
_PYTHON_EXECUTABLE = re.compile(
    r"(?:python|cpython|pypy|graalpy|pyodide)(?:\d+(?:\.\d+)*)?t?(?:@[\s\S]+)?"
)
_SCRIPT_EXECUTABLE = re.compile(r"(?:perl|ruby)(?:\d+(?:\.\d+)*)?")
_UV_DIRECT_COMMANDS = frozenset(
    {"pip", "lock", "sync", "tree", "version", "build", "export", "format"}
)
_UV_VALUE_OPTIONS = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--directory",
        "--env-file",
        "--extra",
        "--no-extra",
        "--group",
        "--no-group",
        "--only-group",
        "--package",
        "--project",
        "--python",
        "--python-preference",
        "--python-platform",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--from",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--index-strategy",
        "--find-links",
        "--keyring-provider",
        "-C",
        "-p",
        "-w",
        "-i",
        "-f",
    }
)
_UV_FLAG_OPTIONS = frozenset(
    {
        "--no-sync",
        "--reinstall",
        "--frozen",
        "--locked",
        "--offline",
        "--no-progress",
        "--no-config",
        "--isolated",
        "--no-project",
        "--no-env-file",
        "--active",
        "--all-packages",
        "--all-extras",
        "--all-groups",
        "--no-default-groups",
        "--no-dev",
        "--only-dev",
        "--no-editable",
        "--exact",
        "--no-index",
        "--no-python-downloads",
        "--managed-python",
        "--no-managed-python",
        "--no-cache",
        "--quiet",
        "--verbose",
        "--system-certs",
        "-n",
        "-q",
        "-v",
    }
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
# Transcript sources whose contract flags every unexecuted tool call with is_error.
_DENIAL_FLAGGING_SOURCES = frozenset({"claude"})

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


@lru_cache(maxsize=512)
def classify_validation_command_equivalence(command: str) -> ValidationCommandEquivalence:
    """Strip approved prefixes and reject wrappers that can obscure exit status."""
    core_command = _strip_exit_preserving_prefixes(command)
    reason = _wrapper_reason(core_command)
    if reason is not None:
        return ValidationCommandEquivalence(None, True, reason)
    return ValidationCommandEquivalence(core_command, False, None)


def wrapped_validation_command(command: object, project_path: str | None = None) -> str | None:
    """Name the wrapper that would void close-gate credit for a validation run.

    Returns ``None`` when the run is credited as it stands, or when the call runs
    nothing the close gate recognizes as validation. The gate's own matcher
    config decides that, resolved with the project's ``.gobby/project.json``
    overrides, so custom matchers count and non-executing forms such as
    ``--collect-only`` or ``--version`` stay quiet. The config is read only once
    a wrapper is present, keeping bare commands free of file reads.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    core = _strip_exit_preserving_prefixes(command)
    reason = _wrapper_reason(core)
    if reason is None:
        return None
    config = resolve_validation_detection_config(project_path=project_path)
    if not is_validation_command(core, config):
        return None
    return reason


def _strip_exit_preserving_prefixes(command: str) -> str:
    return normalize_validation_evidence_command(command)


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
    resolved = _first_executable(parsed)
    if resolved is None:
        return "ambiguous launcher"
    executable, arguments = resolved
    if executable == "nohup":
        return "nohup wrapper"
    if executable in _SHELL_COMMAND_WRAPPERS:
        if not arguments or arguments[0].startswith(("-", "+")):
            return "subshell wrapper"
    if executable == "js_repl":
        return "js_repl wrapper"
    if executable in _NODE_WRAPPERS:
        return "node wrapper"
    if _SCRIPT_EXECUTABLE.fullmatch(executable) and (not arguments or arguments[0].startswith("-")):
        return f"{executable} interpreter wrapper"
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        return _python_wrapper_reason(arguments)
    return None


def _python_wrapper_reason(arguments: tuple[str, ...]) -> str | None:
    """Credit only proven module/script runs; unknown options fail closed."""
    options = iter(arguments)
    for argument in options:
        if argument == "--":
            script = next(options, "")
            return None if script and script != "-" else "ambiguous python execution"
        if argument == "-" or not argument:
            return "ambiguous python execution"
        if not argument.startswith("-"):
            return None  # Everything after the script path is script data.
        if argument == "--check-hash-based-pycs":
            if next(options, None) is None:
                return "ambiguous python execution"
            continue
        # CPython scans combined short options. W/X consume the token suffix,
        # or the next argument when no suffix remains; their operands are data.
        short_options = argument[1:]
        for index, option in enumerate(short_options):
            if option in "cm":
                if option == "c":
                    return "python inline wrapper"
                module = short_options[index + 1 :] or next(options, "")
                return None if module else "ambiguous python execution"
            if option in "WX":
                if index == len(short_options) - 1 and next(options, None) is None:
                    return "ambiguous python execution"
                break
            if option not in "bBdEiIOPqRsStuvx":
                return "unsupported python option"
    return "ambiguous python execution"


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


def _first_executable(parsed: ParsedShellCommand) -> tuple[str, tuple[str, ...]] | None:
    if not parsed.segments or not parsed.segments[0]:
        return None
    tokens = parsed.segments[0]
    while tokens:
        if _ENV_ASSIGNMENT_PREFIX.match(tokens[0]):
            tokens = tokens[1:]
            continue
        executable = os.path.basename(tokens[0]).lower()
        arguments = tokens[1:]
        if executable in {"uv", "uvx"}:
            inner = _uv_run_arguments(arguments if executable == "uv" else ("run", *arguments))
            if inner is None:
                return None
            if inner[0] == "uv" and len(inner) > 1 and inner[1] in _UV_DIRECT_COMMANDS:
                return "uv", inner[1:]  # Proven direct uv subcommand.
            tokens = inner
        elif executable in {
            "timeout",
            "gtimeout",
            "time",
            "exec",
            "command",
            "xargs",
            "stdbuf",
            "sudo",
            "caffeinate",
        }:
            inner = _launcher_arguments(executable, arguments)
            if inner is None:
                return None
            tokens = inner
        elif executable in {"env", "rtk"}:
            if arguments[:1] == ("--",):
                arguments = arguments[1:]
            if arguments and arguments[0].startswith("-"):
                return None
            tokens = arguments
        elif executable == "nice":
            return None  # Normalization already removed every proven nice prefix.
        else:
            return executable, arguments
    return None


def _uv_run_arguments(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    """Resolve only known uv options, stopping at the child command boundary."""
    index = 0
    seen_run = False
    module = False
    while index < len(arguments):
        token = arguments[index]
        if not seen_run and token == "run":
            seen_run = True
            index += 1
            continue
        if seen_run and token == "--":
            child = arguments[index + 1 :]
            return ("python", "-m", *child) if module and child else child or None
        if not token.startswith("-"):
            child = arguments[index:]
            if seen_run:
                return ("python", "-m", *child) if module else child
            if token in _UV_DIRECT_COMMANDS:
                return ("uv", *child)
            return None
        if seen_run and token in {"-m", "--module"}:
            module = True
            index += 1
            continue
        option, equals, value = token.partition("=")
        if option in _UV_VALUE_OPTIONS:
            if equals:
                if not value:
                    return None
                index += 1
            elif index + 1 < len(arguments):
                index += 2
            else:
                return None
        elif token in _UV_FLAG_OPTIONS or re.fullmatch(r"-[qv]+", token):
            index += 1
        elif len(token) > 2 and token[:2] in _UV_VALUE_OPTIONS:
            index += 1  # Attached short-option operand, e.g. -p3.14.
        else:
            return None
    return None


def _launcher_arguments(executable: str, arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    """Resolve bounded prefix options; unknown options never hide the child."""
    if executable == "gtimeout":
        executable = "timeout"
    flags, values = {
        "timeout": (
            {"--foreground", "--preserve-status", "--verbose", "-v"},
            {"-s", "--signal", "-k", "--kill-after"},
        ),
        "time": ({"-p"}, set()),
        "exec": ({"-c", "-l"}, {"-a"}),
        "command": ({"-p"}, set()),
        "stdbuf": (set(), {"-i", "-o", "-e", "--input", "--output", "--error"}),
        "sudo": (
            {"-n", "-E", "-H", "-S", "-k", "--non-interactive", "--preserve-env", "--set-home"},
            {
                "-u",
                "-g",
                "-h",
                "-p",
                "-C",
                "-T",
                "-R",
                "-D",
                "--user",
                "--group",
                "--host",
                "--prompt",
                "--close-from",
                "--command-timeout",
                "--chroot",
                "--chdir",
                "--preserve-env",
            },
        ),
        "caffeinate": ({"-d", "-i", "-m", "-s", "-u"}, {"-t", "-w"}),
        "xargs": (
            {"-0", "-r", "-t", "-x", "--null", "--no-run-if-empty", "--verbose"},
            {
                "-n",
                "-P",
                "-E",
                "-L",
                "-s",
                "--max-args",
                "--max-procs",
                "--eof",
                "--max-lines",
                "--max-chars",
                "-d",
                "--delimiter",
            },
        ),
    }[executable]
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        token = arguments[index]
        if token == "--":
            index += 1
            break
        option, equals, value = token.partition("=")
        if token in flags:
            index += 1
        elif option in values:
            if equals:
                if not value:
                    return None
                index += 1
            elif index + 1 < len(arguments):
                index += 2
            else:
                return None
        elif len(token) > 2 and token[:2] in values:
            index += 1
        else:
            return None
    if executable == "timeout":
        index += 1  # Duration is a launcher operand, never the child executable.
    return arguments[index:] or None


def is_unexecuted_tool_result(result: Any, *, source: str | None = None) -> bool:
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
    # Claude flags every rejection, hook block and permission denial with
    # is_error. An unflagged result ran, and its transport copy (the edited
    # file, the patch context) may quote denial text without being one. Other
    # parsers coerce a missing flag to False, so for them it proves nothing.
    tool_result = result.get("tool_result") if isinstance(result, dict) else None
    if (
        source in _DENIAL_FLAGGING_SOURCES
        and isinstance(tool_result, dict)
        and tool_result.get("is_error") is False
    ):
        return False
    output, _truncated = extract_output(result)
    if not output:
        return False
    return any(pattern.search(output) for pattern in _UNEXECUTED_TOOL_PATTERNS)


def extract_output(result: Any, *, max_chars: int = _OUTPUT_CHAR_LIMIT) -> tuple[str | None, bool]:
    """Extract bounded command output needed to classify validation failures."""
    if isinstance(result, dict) and "outcome_provenance" in result:
        result = {key: value for key, value in result.items() if key != "outcome_provenance"}
    # The provider record around a tool result carries uuids, cwd, branch and
    # slug, none of which is command output (#23787). Of it, only Claude's
    # toolUseResult transport can add output, less its copy of the result, which
    # would otherwise exhaust the output limit.
    if isinstance(result, dict) and "raw_json" in result:
        raw_json = result["raw_json"]
        transport = raw_json.get("toolUseResult") if isinstance(raw_json, dict) else None
        tool_result = result.get("tool_result")
        content = tool_result.get("content") if isinstance(tool_result, dict) else None
        if content and isinstance(content, str) and isinstance(transport, str):
            # A failed Claude Bash call's transport is "Error: " plus the result.
            transport = transport.replace(content, "", 1).removeprefix("Error: ")
        result = {key: value for key, value in result.items() if key != "raw_json"}
        result["toolUseResult"] = transport
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
    if len(output) <= max_chars:
        return output, False
    half = (max_chars - len("\n...[output truncated]...\n")) // 2
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
    if runner_reported_failures(output) and (
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


def _find_exit_code(result: Any) -> int | None:
    for value in _walk_values(result):
        if not isinstance(value, dict):
            continue
        metadata = extract_result_metadata("bash", value)
        candidates = [
            metadata.get("exit_code"),
            *(value.get(key) for key in _EXIT_CODE_KEYS),
            _error_header_exit_code(value),
        ]
        for candidate in candidates:
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    return None


def _error_header_exit_code(value: dict[str, Any]) -> int | None:
    """Claude records a failed Bash status only as the error content's first line."""
    content = value.get("content")
    if value.get("is_error") is not True or not isinstance(content, str):
        return None
    header = _ERROR_EXIT_HEADER.fullmatch(content.split("\n", 1)[0].strip())
    return int(header[1]) if header else None


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
