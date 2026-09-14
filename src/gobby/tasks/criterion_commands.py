"""Explicit criterion commands and their transcript evidence for task close."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import replace

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.config.validation_detection import classify_validation_segments
from gobby.tasks.command_equivalence import (
    canonical_command,
    command_covers,
    parse_validation_shell,
)
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence

_CRITERIA_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
CRITERION_COMMAND_CONTRACT = (
    "Backticked command spans are mandatory exact close commands. "
    "Run each listed command directly after the final edit; pipelines, wrappers, "
    "redirections, and trailing output are not credited."
)
_PLACEHOLDER_RE = re.compile(r"<[^>\s]+>|\{[^{}\s]+\}")
_TRAILING_PUNCTUATION = frozenset(".,;!?")
_CONDITIONAL_TOKENS = frozenset({"if", "for", "while", "case", "until"})
_INCOMPLETE_PACKAGE_PREFIXES = frozenset(
    {
        "bun",
        "bunx",
        "cargo",
        "gobby",
        "git",
        "go",
        "just",
        "make",
        "npm",
        "npx",
        "pnpm",
        "python",
        "python3",
        "uv",
        "yarn",
    }
)
_CRITERION_COMMAND_PREFIXES = frozenset(
    {
        "bash",
        "bun",
        "bunx",
        "cargo",
        "gobby",
        "git",
        "go",
        "just",
        "make",
        "node",
        "npm",
        "npx",
        "pnpm",
        "python",
        "python3",
        "sh",
        "uv",
        "yarn",
        "zsh",
    }
)


def expand_successful_and_segments(
    runs: Iterable[TranscriptValidationRun],
) -> list[TranscriptValidationRun]:
    """Expose each proven validation segment of a successful top-level ``&&`` chain."""
    expanded: list[TranscriptValidationRun] = []
    for run in runs:
        segments = run.validation_segments
        parsed = parse_shell_command(run.command)
        segment_indexes = [segment.segment_index for segment in segments]
        if (
            run.outcome != "success"
            or run.wrapped
            or not parsed.operators
            or set(parsed.operators) != {"&&"}
            or not segments
            or len(set(segment_indexes)) != len(segment_indexes)
            or any(not 0 <= index < len(parsed.segments) for index in segment_indexes)
            or len(parsed.segments) != len(parsed.operators) + 1
        ):
            expanded.append(run)
            continue
        segment_runs = [
            replace(
                run,
                command=shlex.join(parsed.segments[segment.segment_index]),
                categories=segment.categories,
                validation_segments=(segment,),
            )
            for segment in segments
        ]
        if any(
            segment_run.wrapped or segment_run.core_command is None for segment_run in segment_runs
        ):
            expanded.append(run)
            continue
        expanded.extend(segment_runs)
    return expanded


def criterion_command_records(
    criteria: str,
    evidence: TranscriptEvidence,
    *,
    last_edit_order: int | None,
) -> list[dict[str, object]]:
    runs = sorted(
        expand_successful_and_segments((*evidence.validation_runs, *evidence.command_runs)),
        key=lambda item: (item.order, item.completed_at),
    )
    observed_cores = {run.core_command for run in runs if run.core_command is not None}
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for match in _CRITERIA_CODE_SPAN_RE.finditer(criteria):
        if _command_is_excluded(criteria, match):
            continue
        command = match.group(1).strip()
        equivalence = classify_validation_command_equivalence(command)
        core = equivalence.core_command
        if not _looks_like_criterion_command(command, core, observed_cores):
            continue
        key = (canonical_command(core) or core) if core else command
        if key in seen:
            continue
        seen.add(key)
        matching = [
            run
            for run in runs
            if (
                core is not None
                and run.core_command is not None
                and command_covers(run.core_command, core)
            )
            or (core is None and run.command.strip() == command)
        ]
        fresh = [run for run in matching if last_edit_order is None or run.order > last_edit_order]
        definitive = [
            run
            for run in fresh
            if not run.wrapped and run.core_command is not None and run.outcome != "unknown"
        ]
        latest = definitive[-1] if definitive else None
        if latest is not None:
            status = "satisfied" if latest.outcome == "success" else "failed"
            execution = execution_details(latest)
        elif fresh:
            latest = fresh[-1]
            status = "wrapped" if latest.wrapped else "unknown"
            execution = execution_details(latest)
        elif matching:
            latest = matching[-1]
            status = "stale"
            execution = execution_details(latest)
            invalidating_edit = first_invalidating_edit(evidence.edits, latest.order)
            if invalidating_edit is not None:
                execution["invalidating_edit"] = edit_details(invalidating_edit)
        else:
            status = "wrapped" if equivalence.wrapped else "missing"
            execution = None
        record: dict[str, object] = {
            "command": command,
            "core_command": core,
            "status": status,
            "satisfied": status == "satisfied",
        }
        if execution is not None:
            record["execution"] = execution
        if equivalence.wrapper_reason is not None:
            record["wrapper_reason"] = equivalence.wrapper_reason
        observed = []
        for run in runs:
            if run not in matching and not _related_command(run.command, core or command):
                continue
            observation = execution_details(run)
            if last_edit_order is not None and run.order <= last_edit_order:
                edit = first_invalidating_edit(evidence.edits, run.order)
                if edit is not None:
                    observation["invalidating_edit"] = edit_details(edit)
            if run not in matching and not run.wrapped:
                observation["reason"] = "scope or semantic arguments differ"
            observed.append(observation)
        if status != "satisfied":
            record["observed_forms"] = observed
        records.append(record)
    return records


def _command_is_excluded(criteria: str, match: re.Match[str]) -> bool:
    # Mask command contents so dots/semicolons inside shell arguments are not
    # mistaken for prose clause boundaries. Never let another clause negate this one.
    masked = _CRITERIA_CODE_SPAN_RE.sub(lambda span: " " * len(span.group()), criteria)
    boundaries = list(re.finditer(r"[.;!?\n,()]|\b(?:but|however|whereas|and)\b", masked, re.I))
    start = max((part.end() for part in boundaries if part.end() <= match.start()), default=0)
    end = min(
        (part.start() for part in boundaries if part.start() >= match.end()), default=len(criteria)
    )
    clause = masked[start:end].casefold()
    return bool(
        re.search(
            r"\b(?:(?:not|isn't|aren't) (?:required|necessary|applicable|needed)|unnecessary|inapplicable|"
            r"no need (?:to|for)|do not (?:run|require)|need not (?:run|be)|"
            r"does not apply|doesn't apply|can be skipped)\b",
            clause,
        )
    )


def _related_command(observed: str, required: str) -> bool:
    """Find diagnostic forms of an invocation without treating them as credit."""
    if required in observed:
        return True
    expected = parse_validation_shell(required)
    actual = parse_validation_shell(observed)
    if not expected.segments:
        return observed.strip() == required.strip()
    required_tokens = expected.segments[0]
    for segment in actual.segments:
        core = classify_validation_command_equivalence(shlex.join(segment)).core_command
        tokens = safe_split(core or shlex.join(segment))
        # Include changed selectors/flags and pipeline stages; wrapper status still
        # prevents all such observations from becoming definitive executions.
        prefix_size = 3 if required_tokens[:2] == ("uv", "run") else 1
        if tuple(tokens[:prefix_size]) == required_tokens[:prefix_size]:
            return True
    return False


def _looks_like_criterion_command(
    command: str,
    core_command: str | None,
    observed_cores: set[str],
) -> bool:
    if core_command in observed_cores:
        return True
    if classify_validation_segments(command):
        return True
    tokens = safe_split(core_command or command)
    if not tokens:
        return False
    return posixpath.basename(tokens[0]).casefold() in _CRITERION_COMMAND_PREFIXES


def authored_criterion_commands(criteria: str) -> list[str]:
    """Well-formed command-shaped backtick spans that close will require exactly."""
    commands: list[str] = []
    seen: set[str] = set()
    for command in _command_shaped_spans(criteria):
        if _malformed_command_reason(command) is not None or command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands


def malformed_criterion_command_findings(criteria: str) -> tuple[str, ...]:
    """Reject command-shaped spans that cannot be an exact close command."""
    findings: list[str] = []
    seen: set[str] = set()
    for command in _command_shaped_spans(criteria):
        reason = _malformed_command_reason(command)
        if reason is None or command in seen:
            continue
        seen.add(command)
        findings.append(
            f"`{command}`: {reason} Backticked command spans become mandatory exact "
            "close commands; write one direct command or remove the backticks."
        )
    return tuple(findings)


def criterion_command_authoring_payload(criteria: str) -> dict[str, object]:
    """Explain stored exact-command close spans so they are not silent."""
    commands = authored_criterion_commands(criteria)
    if not commands:
        return {}
    return {
        "criterion_commands": commands,
        "criterion_command_contract": CRITERION_COMMAND_CONTRACT,
    }


def _command_shaped_spans(criteria: str) -> list[str]:
    spans: list[str] = []
    for match in _CRITERIA_CODE_SPAN_RE.finditer(criteria):
        if _command_is_excluded(criteria, match):
            continue
        command = match.group(1).strip()
        if command and _is_command_shaped_span(command):
            spans.append(command)
    return spans


def _is_command_shaped_span(command: str) -> bool:
    equivalence = classify_validation_command_equivalence(command)
    if _looks_like_criterion_command(command, equivalence.core_command, set()):
        return True
    tokens = safe_split(command)
    if not tokens:
        return False
    names = {posixpath.basename(token).casefold() for token in tokens}
    return bool(names & _CRITERION_COMMAND_PREFIXES) or tokens[0].casefold() in _CONDITIONAL_TOKENS


def _malformed_command_reason(command: str) -> str | None:
    if _PLACEHOLDER_RE.search(command):
        return "placeholders are not an exact close command."
    stripped = command.rstrip()
    if stripped and stripped[-1] in _TRAILING_PUNCTUATION:
        return "trailing punctuation is not part of an exact close command."
    parsed = parse_shell_command(command)
    if any(op in {"|", "||", "|&"} for op in parsed.operators):
        return "pipelines are not an exact close command."
    if _has_redirection(command):
        return "redirections are not an exact close command."
    tokens = safe_split(command)
    if tokens and tokens[0].casefold() in _CONDITIONAL_TOKENS:
        return "conditional commands are not an exact close command."
    prefix = posixpath.basename(tokens[0]).casefold() if tokens else ""
    if prefix in _INCOMPLETE_PACKAGE_PREFIXES and len(tokens) < 2:
        return "incomplete package command is not an exact close command."
    return None


def _has_redirection(command: str) -> bool:
    remainder = _PLACEHOLDER_RE.sub("", command)
    return bool(re.search(r"(?:>>?|[12]>|<)", remainder))


def execution_details(run: TranscriptValidationRun) -> dict[str, object]:
    details: dict[str, object] = {
        "session_id": run.session_id,
        "source": run.source,
        "command": run.command,
        "core_command": run.core_command,
        "completed_at": run.completed_at.isoformat(),
        "order": run.order,
        "outcome": run.outcome,
        "exit_code": run.exit_code,
    }
    if run.wrapper_reason is not None:
        details["wrapper_reason"] = run.wrapper_reason
    if run.unknown_reason is not None:
        details["unknown_reason"] = run.unknown_reason
    return details


def edit_details(edit: TranscriptEdit) -> dict[str, object]:
    return {
        "session_id": edit.session_id,
        "source": edit.source,
        "path": edit.path,
        "timestamp": edit.timestamp.isoformat(),
        "order": edit.order,
        "tool_name": edit.tool_name,
    }


def first_invalidating_edit(
    edits: Iterable[TranscriptEdit], run_order: int
) -> TranscriptEdit | None:
    return min(
        (edit for edit in edits if edit.order > run_order),
        key=lambda edit: (edit.order, edit.timestamp, edit.path),
        default=None,
    )


def criterion_command_gap_message(gaps: list[dict[str, object]]) -> str:
    actions: list[str] = []
    for gap in gaps:
        command = str(gap.get("core_command") or gap["command"])
        status = str(gap["status"])
        execution = gap.get("execution")
        reason = status
        if status == "stale" and isinstance(execution, Mapping):
            invalidating_edit = execution.get("invalidating_edit")
            if isinstance(invalidating_edit, Mapping):
                reason = (
                    f"execution order {execution['order']} at {execution['completed_at']} was "
                    f"invalidated by {invalidating_edit['path']} at order "
                    f"{invalidating_edit['order']} ({invalidating_edit['timestamp']})"
                )
        actions.append(f"Run `{command}` clean after the final task edit ({reason}).")
        observed = gap.get("observed_forms")
        if isinstance(observed, list):
            for form in observed:
                if not isinstance(form, Mapping):
                    continue
                explanation = (
                    form.get("wrapper_reason")
                    or form.get("unknown_reason")
                    or form.get("reason")
                    or form.get("outcome")
                )
                stale = form.get("invalidating_edit")
                if isinstance(stale, Mapping):
                    explanation = f"{explanation}; invalidated by {stale['path']} at order {stale['order']} ({stale['timestamp']})"
                actions.append(f"Observed `{form['command']}`: {explanation}.")
    return "Required criterion commands are unsatisfied: " + " ".join(actions)
