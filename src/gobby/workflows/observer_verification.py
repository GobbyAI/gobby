"""Verification-evidence observer for workflow session variables."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.config.validation_detection import (
    ValidationCommandMatch,
    classify_validation_command,
    resolve_validation_detection_config,
    shell_command_segments,
)
from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _json_safe,
    _shell_tool_outcome,
)
from gobby.workflows.verification_evidence import (
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE,
    VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND,
    VERIFICATION_EVIDENCE_VARIABLE,
    append_verification_evidence,
    correlate_validation_command_result,
)

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.workflows.observers")

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PYTEST_COUNT_RE = re.compile(
    r"\b(?P<count>\d+)\s+(?P<result>failed|errors?|passed|skipped|xfailed|xpassed)\b"
)
_PYTEST_DURATION_RE = re.compile(r"\bin\s+\d+(?:\.\d+)?s(?:\s|=|$)")
_RUFF_CHECK_FAILURE_RE = re.compile(r"Found (?P<count>\d+) errors?(?: \([^)]*\))?\.")
_RUFF_FORMAT_SUCCESS_RE = re.compile(r"(?P<count>\d+) files? already formatted")
_RUFF_FORMAT_FAILURE_RE = re.compile(
    r"(?P<count>\d+) files? would be reformatted"
    r"(?:, \d+ files? already formatted)?"
)
_MYPY_SUCCESS_RE = re.compile(r"Success: no issues found in \d+ source files?")
_MYPY_FAILURE_RE = re.compile(r"Found (?P<count>\d+) errors? in \d+ files?(?: \(.+\))?")
_COMMAND_STRING_WRAPPERS = frozenset(
    {
        "bash-c",
        "bash-lc",
        "fish-c",
        "rust-token-killer-command-string",
        "sh-c",
        "zsh-c",
    }
)
_PIPE_OPERATORS = frozenset({"|", "|&"})


def _extract_shell_output(event: HookEvent) -> str:
    if not event.data:
        return ""
    for field in ("tool_output", "tool_result", "tool_response", "contentItems"):
        output = _extract_shell_output_text(event.data.get(field))
        if output:
            return output
    return ""


def _stdin_only_head_or_tail(tokens: list[str]) -> bool:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-n", "--lines", "-c", "--bytes"}:
            index += 2
            if index > len(tokens):
                return False
            continue
        if token == "--":
            return all(operand == "-" for operand in tokens[index + 1 :])
        if token == "-" or re.fullmatch(r"-(?:\d+|[nc]\d+)", token):
            index += 1
            continue
        if token.startswith(("--lines=", "--bytes=")):
            index += 1
            continue
        return False
    return True


def _summary_preserving_sink(tokens: list[str]) -> bool:
    if not tokens:
        return False
    command = tokens[0].rsplit("/", 1)[-1]
    if command == "tee":
        return True
    if command == "cat":
        return all(token in {"-", "--"} for token in tokens[1:])
    if command in {"head", "tail"}:
        return _stdin_only_head_or_tail(tokens)
    return False


def _pipeline_preserves_summary(match: ValidationCommandMatch) -> bool:
    if not match.shell_operators or any(
        operator not in _PIPE_OPERATORS for operator in match.shell_operators
    ):
        return False
    if any(wrapper in _COMMAND_STRING_WRAPPERS for wrapper in match.wrapper_chain):
        return False

    segments = shell_command_segments(match.command)
    if len(segments) != match.segment_count or len(match.shell_operators) != len(segments) - 1:
        return False
    if match.segment_index >= len(segments) - 1:
        return False
    return all(_summary_preserving_sink(tokens) for tokens in segments[match.segment_index + 1 :])


def _final_summary_line(output: str) -> str:
    clean_output = _ANSI_ESCAPE_RE.sub("", output)
    return next((line.strip() for line in reversed(clean_output.splitlines()) if line.strip()), "")


def _pytest_summary_outcome(line: str) -> bool | None:
    lowered = line.lower()
    counts = [
        (int(match.group("count")), match.group("result"))
        for match in _PYTEST_COUNT_RE.finditer(lowered)
    ]
    no_tests = "no tests ran" in lowered or "no tests collected" in lowered
    if no_tests:
        return None if any(count for count, _ in counts) else False
    if not _PYTEST_DURATION_RE.search(lowered) or not counts:
        return None
    if any(count and result in {"failed", "error", "errors"} for count, result in counts):
        return False
    if any(
        count and result in {"passed", "skipped", "xfailed", "xpassed"} for count, result in counts
    ):
        return True
    return None


def _ruff_summary_outcome(line: str) -> bool | None:
    if line == "All checks passed!":
        return True
    failure_match = _RUFF_CHECK_FAILURE_RE.fullmatch(line)
    if failure_match:
        return False if int(failure_match.group("count")) else None
    success_match = _RUFF_FORMAT_SUCCESS_RE.fullmatch(line)
    if success_match:
        return True if int(success_match.group("count")) else None
    format_failure_match = _RUFF_FORMAT_FAILURE_RE.fullmatch(line)
    if format_failure_match:
        return False if int(format_failure_match.group("count")) else None
    return None


def _mypy_summary_outcome(line: str) -> bool | None:
    if _MYPY_SUCCESS_RE.fullmatch(line):
        return True
    failure_match = _MYPY_FAILURE_RE.fullmatch(line)
    if failure_match:
        return False if int(failure_match.group("count")) else None
    return None


def _validator_command(argv: tuple[str, ...]) -> tuple[str, list[str]]:
    if not argv:
        return "", []
    command = argv[0].rsplit("/", 1)[-1]
    if command.startswith("python") and len(argv) >= 3 and argv[1] == "-m":
        return argv[2], list(argv[3:])
    return command, list(argv[1:])


def _summary_outcome(match: ValidationCommandMatch, output: str) -> tuple[bool | None, str | None]:
    line = _final_summary_line(output)
    if not line:
        return None, None
    if match.matcher_id == "python-tests":
        command, arguments = _validator_command(match.normalized_argv)
        if command == "pytest" or (
            command == "coverage" and arguments[:3] == ["run", "-m", "pytest"]
        ):
            success = _pytest_summary_outcome(line)
            return success, "validation_summary.pytest" if success is not None else None
        return None, None

    command, _ = _validator_command(match.normalized_argv)
    if command == "ruff":
        success = _ruff_summary_outcome(line)
        return success, "validation_summary.ruff" if success is not None else None
    if command == "mypy":
        success = _mypy_summary_outcome(line)
        return success, "validation_summary.mypy" if success is not None else None
    return None, None


def _validation_segment_outcome(
    match: ValidationCommandMatch,
    aggregate_outcome: bool | None,
    output: str,
) -> tuple[bool | None, str | None]:
    """Resolve aggregate shell status to the matched validation segment."""
    if match.evidence_requires_confirmation:
        return None, None
    if not match.is_compound:
        return aggregate_outcome, None
    if (
        aggregate_outcome is True
        and match.shell_operators
        and all(operator == "&&" for operator in match.shell_operators)
    ):
        return True, None
    if _pipeline_preserves_summary(match):
        return _summary_outcome(match, output)
    return None, None


def detect_verification_evidence(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    daemon_config: Any | None = None,
) -> None:
    """Record validation-command evidence from shell tool runs."""
    if not event.data:
        return

    tool_name = event.data.get("tool_name", "")
    if tool_name not in _SHELL_TOOLS:
        return

    command = _extract_shell_command(event)
    detection_config = resolve_validation_detection_config(
        daemon_config=daemon_config,
        project_path=event.metadata.get("project_path") or event.cwd,
    )
    match = classify_validation_command(command, detection_config)
    if match is None:
        return

    outcome = _shell_tool_outcome(event)
    success, summary_provenance = _validation_segment_outcome(
        match,
        outcome.succeeded,
        _extract_shell_output(event),
    )
    evidence = {
        "evidence_type": VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND,
        "command": command,
        "cwd": event.cwd,
        "project_path": event.metadata.get("project_path"),
        "matcher_id": match.matcher_id,
        "matcher_label": match.label,
        "categories": list(match.categories),
        "languages": list(match.languages),
        "normalized_command": match.normalized_command,
        "normalized_argv": list(match.normalized_argv),
        "wrapper_chain": list(match.wrapper_chain),
        "segment_index": match.segment_index,
        "segment_count": match.segment_count,
        "shell_operators": list(match.shell_operators),
        "evidence_requires_confirmation": match.evidence_requires_confirmation,
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_name": tool_name,
        "success": success,
    }
    if outcome.exit_code is not None:
        evidence["exit_code"] = outcome.exit_code
    outcome_provenance = summary_provenance or outcome.provenance
    if outcome_provenance is not None:
        evidence["outcome_provenance"] = outcome_provenance
    correlated = correlate_validation_command_result(evidence)
    evidence["success"] = correlated.success if correlated is not None else None
    existing = variables.get(VERIFICATION_EVIDENCE_VARIABLE, [])
    variables[VERIFICATION_EVIDENCE_VARIABLE] = append_verification_evidence(
        existing, _json_safe(evidence), session_id=session_id
    )

    if correlated is not None and correlated.success:
        variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = True
        logger.debug(
            "Session %s: verification_evidence_recorded=true via validation command",
            session_id,
        )
        return

    if correlated is not None and not correlated.success:
        variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = False
        logger.info(
            "Session %s: verification readiness cleared after failed validation command",
            session_id,
        )
        return

    logger.debug(
        "Session %s: verification readiness unchanged after validation command with unknown outcome",
        session_id,
    )
