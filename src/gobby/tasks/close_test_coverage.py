"""Lexical coverage of changed Python tests for the close validation gate."""

from __future__ import annotations

import posixpath
from collections.abc import Iterable

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.tasks.command_equivalence import pytest_targets
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun

_TEST_TYPES_AUDIT_MATCHER = "gobby-test-types-audit"
_TEST_TYPES_BASELINE = ".gobby/test-types-baseline.json"
# Flags that change only how the audit reports. ``--min-severity`` cannot weaken the
# ratchet: its default is already the loosest choice, so an explicit value is at least
# as strict. Everything else -- ``--write-baseline``, ``--allow-failing-baseline``,
# ``--mypy-command`` -- changes what the audit enforces and still voids credit.
_TEST_TYPES_OUTPUT_ONLY_FLAGS = frozenset({"--format", "--output", "--min-severity"})


def changed_python_test_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    python_tests: set[str] = set()
    for path in changed_paths:
        normalized = _normalize_repo_path(path)
        if (
            normalized is not None
            and normalized.startswith("tests/")
            and normalized.endswith(".py")
        ):
            python_tests.add(normalized)
    return tuple(sorted(python_tests))


def test_types_audit_targets(run: TranscriptValidationRun) -> tuple[str, ...] | None:
    if run.matcher_id != _TEST_TYPES_AUDIT_MATCHER:
        return None
    core_command = run.core_command or run.command
    if len(parse_shell_command(core_command).segments) != 1:
        return None
    commands = [segment.command for segment in run.validation_segments]
    if not commands:
        commands = [core_command]
    for command in commands:
        targets = _test_types_command_targets(command)
        if targets is not None:
            return targets
    return None


def uncovered_test_paths(
    changed_python_tests: tuple[str, ...],
    audit_targets: tuple[str, ...],
) -> tuple[str, ...]:
    """Return changed tests outside every lexical target, including missing paths."""
    return tuple(
        path
        for path in changed_python_tests
        if not any(
            target == "." or path == target or path.startswith(f"{target}/")
            for target in audit_targets
        )
    )


def uncovered_pytest_paths(
    commands: Iterable[str],
    changed_python_tests: tuple[str, ...],
) -> tuple[str, ...]:
    """Return changed tests no successful pytest command targets."""
    covered: list[str] = []
    for command in commands:
        targets = pytest_targets(command)
        if targets:
            covered.extend(targets)
    return uncovered_test_paths(changed_python_tests, tuple(covered))


def _test_types_command_targets(command: str) -> tuple[str, ...] | None:
    tokens = safe_split(command)
    prefix = ["gobby", "test-types", "audit"]
    try:
        start = next(
            index
            for index in range(len(tokens) - len(prefix) + 1)
            if tokens[index : index + len(prefix)] == prefix
        )
    except StopIteration:
        return None

    arguments = tokens[start + len(prefix) :]
    targets: list[str] = []
    baselines: list[str] = []
    fail_on_new = 0
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--baseline":
            index += 1
            if index >= len(arguments):
                return None
            baselines.append(arguments[index])
        elif argument.startswith("--baseline="):
            baselines.append(argument.partition("=")[2])
        elif argument == "--fail-on-new":
            fail_on_new += 1
        elif argument in _TEST_TYPES_OUTPUT_ONLY_FLAGS:
            # Consume the flag's value so it is never mistaken for a target.
            index += 1
            if index >= len(arguments):
                return None
        elif argument.partition("=")[0] in _TEST_TYPES_OUTPUT_ONLY_FLAGS:
            pass  # `--flag=value` carries its value inline.
        elif argument.startswith("-"):
            return None
        else:
            targets.append(argument)
        index += 1
    if baselines != [_TEST_TYPES_BASELINE] or fail_on_new != 1 or not targets:
        return None
    normalized_targets: list[str] = []
    for target in targets:
        normalized = _normalize_repo_path(target)
        if normalized is None:
            return None
        normalized_targets.append(normalized)
    return tuple(normalized_targets)


def _normalize_repo_path(path: str) -> str | None:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized
