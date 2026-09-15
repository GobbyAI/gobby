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
    scope_difference,
)
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence

SCOPE_MISMATCH_REASON = "scope or semantic arguments differ"
_CRITERIA_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
CRITERION_COMMAND_CONTRACT = (
    "Backticked command spans are mandatory exact close commands. "
    "Run each listed command directly after the final edit; pipelines, wrappers, "
    "redirections, and trailing output are not credited."
)
_PLACEHOLDER_RE = re.compile(r"<[^>\s]+>|\{[^{}\s]+\}")
_TRAILING_PUNCTUATION = frozenset(".,;!?")
_CONDITIONAL_TOKENS = frozenset({"if", "for", "while", "case", "until"})
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
) -> list[dict[str, object]]:
    # Judge each top-level run before splitting it, so a chain's segments share its freshness.
    invalidating = {
        segment_run: first_invalidating_edit(evidence, run)
        for run in (*evidence.validation_runs, *evidence.command_runs)
        for segment_run in expand_successful_and_segments((run,))
    }
    runs = sorted(invalidating, key=lambda item: (item.order, item.completed_at))
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
        fresh = [run for run in matching if invalidating[run] is None]
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
            invalidating_edit = invalidating[latest]
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
            edit = invalidating[run]
            if edit is not None:
                observation["invalidating_edit"] = edit_details(edit)
            if run not in matching and not run.wrapped:
                difference = scope_difference(run.core_command or run.command, core or command)
                observation["reason"] = f"{SCOPE_MISMATCH_REASON}: {difference}"
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
    if len(tokens) < 2:
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
        if command and (_is_command_shaped_span(command) or _full_suite_runner(command)):
            spans.append(command)
    return spans


def _is_command_shaped_span(command: str) -> bool:
    tokens = safe_split(command)
    if len(tokens) < 2:
        return False
    equivalence = classify_validation_command_equivalence(command)
    if _looks_like_criterion_command(command, equivalence.core_command, set()):
        return True
    names = {posixpath.basename(token).casefold() for token in tokens}
    return bool(names & _CRITERION_COMMAND_PREFIXES) or tokens[0].casefold() in _CONDITIONAL_TOKENS


def _malformed_command_reason(command: str) -> str | None:
    if _PLACEHOLDER_RE.search(command):
        return "placeholders are not an exact close command."
    stripped = command.rstrip()
    # Go package patterns end in "/..."; that is an argument, not sentence punctuation.
    if stripped and stripped[-1] in _TRAILING_PUNCTUATION and not stripped.endswith("/..."):
        return "trailing punctuation is not part of an exact close command."
    parsed = parse_shell_command(command)
    if any(op in {"|", "||", "|&"} for op in parsed.operators):
        return "pipelines are not an exact close command."
    if _has_redirection(command):
        return "redirections are not an exact close command."
    tokens = safe_split(command)
    if tokens and tokens[0].casefold() in _CONDITIONAL_TOKENS:
        return "conditional commands are not an exact close command."
    runner = _full_suite_runner(command)
    if runner is not None:
        return (
            f"{runner} with no target runs the full suite; name test files, a test "
            "directory, or a test-name selector."
        )
    return None


_RUNNER_LAUNCHERS = (
    ("uv", "run"),
    ("python", "-m"),
    ("python3", "-m"),
    ("npm", "exec"),
    ("pnpm", "exec"),
    ("npx",),
    ("pnpm",),
    ("yarn",),
)
_PYTEST_SUBDIRECTORY_RE = re.compile(r"(?:^|/)tests/[^/\s]+")
_JS_TEST_FILE_RE = re.compile(r"(?:__tests__/\S+|\.(?:test|spec))\.(?:ts|js|tsx|jsx)\b")
_JS_TEST_NAME_OPTIONS = frozenset({"-t", "--testNamePattern", "--testPathPattern"})


def _full_suite_runner(command: str) -> str | None:
    """Name the runner any segment of a span would start with no test target.

    Mirrors the no-full-* rules, which select each executable segment separately.
    """
    for segment in parse_shell_command(command).segments:
        runner = _segment_full_suite_runner(shlex.join(segment))
        if runner is not None:
            return runner
    return None


def _segment_full_suite_runner(command: str) -> str | None:
    core = classify_validation_command_equivalence(command).core_command
    tokens = list(safe_split(core or command))
    for launcher in _RUNNER_LAUNCHERS:
        if tuple(tokens[: len(launcher)]) == launcher:
            tokens = tokens[len(launcher) :]
    if not tokens:
        return None
    runner = posixpath.basename(tokens[0])
    arguments = tokens[1:]
    if runner == "pytest":
        targeted = any(
            argument.startswith("-k")
            or (
                not argument.startswith("-")
                and (
                    argument.split("::", 1)[0].endswith(".py")
                    or _PYTEST_SUBDIRECTORY_RE.search(argument) is not None
                )
            )
            for argument in arguments
        )
    elif runner in {"vitest", "jest"}:
        targeted = any(
            argument.split("=", 1)[0] in _JS_TEST_NAME_OPTIONS
            or _JS_TEST_FILE_RE.search(argument) is not None
            for argument in arguments
        )
    elif runner == "cargo":
        if arguments[:1] and arguments[0].startswith("+"):
            arguments = arguments[1:]
        if arguments[:1] != ["test"]:
            return None
        runner = "cargo test"
        targeted = any(not argument.startswith("-") for argument in arguments[1:])
    elif runner == "go":
        if arguments[:1] != ["test"]:
            return None
        runner = "go test"
        packages = [argument for argument in arguments[1:] if not argument.startswith("-")]
        targeted = bool(packages) and "./..." not in packages
    else:
        return None
    return None if targeted else runner


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


# Configs any tool may read; mirrors `is_data_language` in crates/gcode/src/index/languages.rs.
_DATA_LANGUAGES = frozenset({"yaml", "json"})


def first_invalidating_edit(
    evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> TranscriptEdit | None:
    """Return the first later task edit that can change ``run``'s result.

    Every later edit invalidates a run, except an edit in a known, non-data code
    language outside the languages of the run's single bounded, non-test validation
    segment. Paths the code index does not know have no language and always invalidate.
    """
    readable = _bounded_languages(run)

    def affects(edit: TranscriptEdit) -> bool:
        language = evidence.edit_languages.get(edit.path)
        return (
            readable is None
            or language is None
            or language in _DATA_LANGUAGES
            or language in readable
        )

    return min(
        (edit for edit in evidence.edits if edit.order > run.order and affects(edit)),
        key=lambda edit: (edit.order, edit.timestamp, edit.path),
        default=None,
    )


def _bounded_languages(run: TranscriptValidationRun) -> frozenset[str] | None:
    """Return the languages a run can read, or None when its inputs are unbounded."""
    if run.wrapped or run.core_command is None or len(run.validation_segments) != 1:
        return None
    segment = run.validation_segments[0]
    if not segment.bounded_inputs or not segment.languages or "test" in segment.categories:
        return None
    if len(parse_shell_command(run.core_command).segments) != 1:
        return None
    return frozenset(segment.languages)


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
