"""Extract Git's shell-executed values for the approval guard's subject scan."""

from __future__ import annotations

import shlex
from typing import NamedTuple

from gobby.hooks._shell_execution_words import _unwrap

from .condition_helpers import _GIT_GLOBAL_FLAGS, _GIT_GLOBAL_VALUE_OPTIONS


class _GitShellValues(NamedTuple):
    commands: dict[str, tuple[str, bool]]
    options: dict[str, frozenset[str]]
    config_keys: frozenset[str]
    config_prefixes: dict[str, str]
    environment: frozenset[str]


_GIT_SHELL_VALUES = _GitShellValues(
    # The boolean distinguishes shell strings from commands executed as argv.
    commands={"submodule": ("foreach", True), "bisect": ("run", False)},
    options={
        "filter-branch": frozenset(
            {
                "--setup",
                "--env-filter",
                "--tree-filter",
                "--index-filter",
                "--parent-filter",
                "--msg-filter",
                "--commit-filter",
                "--tag-name-filter",
            }
        ),
        "rebase": frozenset({"-x", "--exec"}),
        "clone": frozenset({"-u", "--upload-pack"}),
        "fetch": frozenset({"--upload-pack"}),
        "pull": frozenset({"--upload-pack"}),
        "ls-remote": frozenset({"--upload-pack"}),
        "push": frozenset({"--receive-pack", "--exec"}),
        "archive": frozenset({"--exec"}),
        "difftool": frozenset({"-x", "--extcmd"}),
        "grep": frozenset({"-O", "--open-files-in-pager"}),
    },
    config_keys=frozenset(
        {
            "core.sshcommand",
            "core.pager",
            "core.editor",
            "core.fsmonitor",
            "sequence.editor",
            "diff.external",
            "credential.helper",
            "gpg.program",
        }
    ),
    config_prefixes={"pager.": "", "alias.": "!"},
    environment=frozenset(
        {"GIT_SSH_COMMAND", "GIT_PAGER", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_EXTERNAL_DIFF"}
    ),
)


def _config_script(assignment: str) -> str | None:
    key, separator, value = assignment.partition("=")
    if not separator:
        return None
    key = key.lower()
    if key in _GIT_SHELL_VALUES.config_keys:
        return value
    for prefix, required in _GIT_SHELL_VALUES.config_prefixes.items():
        if key.startswith(prefix) and key != prefix and value.startswith(required):
            return value[len(required) :]
    return None


def _git_prefix(argv: list[str], scripts: list[str]) -> int:
    cursor = 1
    while cursor < len(argv):
        word = argv[cursor]
        assignment: str | None = None
        if word == "-c" and cursor + 1 < len(argv):
            assignment = argv[cursor + 1]
        elif word.startswith("-c") and word != "-c":
            assignment = word[2:]
        if assignment is not None:
            script = _config_script(assignment)
            if script is not None:
                scripts.append(script)
        if word in _GIT_GLOBAL_VALUE_OPTIONS:
            cursor += 2
        elif (
            word in _GIT_GLOBAL_FLAGS
            or word.startswith("-c")
            or any(word.startswith(f"{option}=") for option in _GIT_GLOBAL_VALUE_OPTIONS)
        ):
            cursor += 1
        elif word == "--":
            return cursor + 1
        else:
            break
    return cursor


def _command_scripts(subcommand: str, args: list[str]) -> list[str]:
    scripts: list[str] = []
    command = _GIT_SHELL_VALUES.commands.get(subcommand)
    if command is not None:
        action, shell_string = command
        cursor = 0
        if subcommand == "submodule":
            while cursor < len(args) and args[cursor] in {"--quiet", "-q"}:
                cursor += 1
        if args[cursor : cursor + 1] == [action]:
            cursor += 1
            if subcommand == "submodule":
                while cursor < len(args) and args[cursor] in {"--recursive", "--quiet", "-q"}:
                    cursor += 1
            target = args[cursor:]
            scripts.append(" ".join(target) if shell_string else shlex.join(target))
    options = _GIT_SHELL_VALUES.options.get(subcommand, frozenset())
    cursor = 0
    while cursor < len(args):
        word = args[cursor]
        if word == "--":
            break
        for option in options:
            if word == option and cursor + 1 < len(args):
                cursor += 1
                scripts.append(args[cursor])
                break
            if word.startswith(option + "="):
                scripts.append(word[len(option) + 1 :])
                break
            if len(option) == 2 and word.startswith(option) and len(word) > 2:
                scripts.append(word[2:])
                break
        cursor += 1
    return scripts


def git_shell_scripts(stages: list[list[str]]) -> list[str]:
    """Return executable Git values, never ordinary quoted option/operand data."""
    scripts: list[str] = []
    for stage in stages:
        argv = _unwrap(stage)
        if not argv or argv[0].rsplit("/", 1)[-1] != "git":
            continue
        for word in stage[: len(stage) - len(argv)]:
            key, separator, value = word.partition("=")
            if separator and key in _GIT_SHELL_VALUES.environment:
                scripts.append(value)
        cursor = _git_prefix(argv, scripts)
        if cursor < len(argv):
            scripts.extend(_command_scripts(argv[cursor], argv[cursor + 1 :]))
    return scripts
