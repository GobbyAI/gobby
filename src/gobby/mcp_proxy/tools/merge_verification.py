"""Environment and command policy for merge verification subprocesses."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from gobby.config.validation_detection import (
    ValidationCommandMatch,
    classify_validation_command,
    resolve_validation_detection_config,
)

_ALLOWED_GIT_VERIFICATION_SUBCOMMANDS = {"diff", "ls-files", "rev-parse", "status"}


_BLOCKED_GIT_DIFF_FLAGS = {"--ext-diff", "--no-index"}


_TEST_SCOPE_FLAGS = {
    "--filter",
    "--grep",
    "--include",
    "--package",
    "--run",
    "--test",
    "--test-name-pattern",
    "--test-path-pattern",
    "--testNamePattern",
    "--testPathPattern",
    "-k",
    "-m",
    "-p",
    "-t",
}


_UNSCOPED_TEST_TARGETS = {".", "./", "...", "./...", "all"}


_TEST_COMMAND_WORDS = {
    "exec",
    "jest",
    "nextest",
    "phpunit",
    "pytest",
    "rspec",
    "run",
    "test",
    "tests",
    "vitest",
}


_SAFE_ENV_KEYS = {"HOME", "LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"}


_BLOCKED_VERIFICATION_ENV_KEYS = {
    "BASH_ENV",
    "ENV",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
}


_BLOCKED_VERIFICATION_ENV_PREFIXES = ("DYLD_",)


def verification_environment() -> dict[str, str]:
    """Return a minimal environment for verification subprocesses."""
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    env["CI"] = "1"
    if "NO_COLOR" in os.environ:
        env["NO_COLOR"] = os.environ["NO_COLOR"]
    return env


def _is_env_assignment(token: str) -> bool:
    key, separator, _ = token.partition("=")
    if not separator or not key:
        return False
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in key)


def _reject_verification_env_key(key: str) -> str | None:
    if key in _BLOCKED_VERIFICATION_ENV_KEYS:
        return f"verification environment variable '{key}' is not permitted"
    if any(key.startswith(prefix) for prefix in _BLOCKED_VERIFICATION_ENV_PREFIXES):
        return f"verification environment variable '{key}' is not permitted"
    return None


def _consume_env_assignments(tokens: list[str]) -> tuple[dict[str, str], list[str], str | None]:
    env: dict[str, str] = {}
    index = 0
    while index < len(tokens) and _is_env_assignment(tokens[index]):
        key, _, value = tokens[index].partition("=")
        rejection = _reject_verification_env_key(key)
        if rejection:
            return {}, [], rejection
        env[key] = value
        index += 1

    return env, tokens[index:], None


def normalize_verification_command(
    argv: list[str],
) -> tuple[list[str], dict[str, str], str | None]:
    if argv[0] == "env":
        env, command_argv, rejection = _consume_env_assignments(argv[1:])
        if rejection:
            return [], {}, rejection
        if not env:
            return [], {}, "env wrapper requires KEY=VALUE assignments before the command"
        if not command_argv:
            return [], {}, "verification command is required after environment assignments"
        return command_argv, env, None

    env, command_argv, rejection = _consume_env_assignments(argv)
    if rejection:
        return [], {}, rejection
    if env and not command_argv:
        return [], {}, "verification command is required after environment assignments"
    return command_argv, env, None


def _reject_git_verification_args(args: list[str]) -> str | None:
    if not args:
        return "git verification requires a subcommand"

    subcommand = args[0]
    if subcommand not in _ALLOWED_GIT_VERIFICATION_SUBCOMMANDS:
        return f"git subcommand '{subcommand}' is not permitted for verification"

    if subcommand == "diff":
        for arg in args[1:]:
            if arg in _BLOCKED_GIT_DIFF_FLAGS or arg.startswith("--output"):
                return f"git diff flag '{arg}' is not permitted for verification"
    return None


def _looks_like_test_execution(argv: tuple[str, ...]) -> bool:
    for token in argv:
        word = Path(token).name
        if word in _TEST_COMMAND_WORDS:
            return True
        if word.endswith("test") or word.endswith("tests"):
            return True
    return False


def _has_test_scope(argv: tuple[str, ...]) -> bool:
    for index, token in enumerate(argv):
        if token in _TEST_SCOPE_FLAGS:
            return index + 1 < len(argv) and argv[index + 1] not in _UNSCOPED_TEST_TARGETS
        for flag in _TEST_SCOPE_FLAGS:
            if token.startswith(f"{flag}="):
                value = token.split("=", 1)[1]
                return bool(value and value not in _UNSCOPED_TEST_TARGETS)

    for token in argv[1:]:
        if token == "--":
            continue
        if token in _UNSCOPED_TEST_TARGETS:
            return False
        if token.startswith("-"):
            continue
        if token in _TEST_COMMAND_WORDS:
            continue
        return True
    return False


def _reject_unscoped_test_command(match: ValidationCommandMatch) -> str | None:
    if "test" not in match.categories:
        return None
    if not _looks_like_test_execution(match.normalized_argv):
        return None
    if _has_test_scope(match.normalized_argv):
        return None
    return "test verification must target a package, path, file, or filter"


def reject_verification_command(argv: list[str], project_path: str) -> str | None:
    executable = Path(argv[0]).name
    if executable == "git":
        return _reject_git_verification_args(argv[1:])

    config = resolve_validation_detection_config(project_path=project_path)
    command = shlex.join(argv)
    match = classify_validation_command(command, config)
    if match is None:
        return f"verification command {command!r} is not a recognized validation command"
    return _reject_unscoped_test_command(match)
