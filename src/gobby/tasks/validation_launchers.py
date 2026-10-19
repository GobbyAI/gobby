"""Resolve validation launcher children and their directory-changing options."""

from __future__ import annotations

import os
import re

from gobby.config.shell_lexing import ParsedShellCommand
from gobby.config.validation_detection import (
    VALIDATION_LAUNCHER_HEADS,
    unwrap_env_tokens,
    unwrap_validation_launcher_tokens,
)

ENV_ASSIGNMENT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHELL_COMMAND_WRAPPERS = {
    "bash",
    "fish",
    "sh",
    "zsh",
    "dash",
    "ksh",
    "mksh",
    "ash",
    "yash",
    "csh",
    "tcsh",
}
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
        "--link-mode",
        "--prerelease",
        "--exclude-newer",
        "--no-binary-package",
        "--resolution",
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


def first_executable(
    parsed: ParsedShellCommand, *, directories: list[str | None] | None = None
) -> tuple[str, tuple[str, ...]] | None:
    if not parsed.segments or not parsed.segments[0]:
        return None
    tokens = parsed.segments[0]
    while tokens:
        if ENV_ASSIGNMENT_PREFIX.match(tokens[0]):
            tokens = tokens[1:]
            continue
        executable = os.path.basename(tokens[0]).lower()
        arguments = tokens[1:]
        if executable == "busybox" and arguments and arguments[0].lower() in SHELL_COMMAND_WRAPPERS:
            tokens = arguments
            continue
        if executable in {"uv", "uvx"}:
            inner = _uv_run_arguments(arguments if executable == "uv" else ("run", *arguments))
            if inner is None:
                return None
            if inner[0] == "uv" and len(inner) > 1 and inner[1] in _UV_DIRECT_COMMANDS:
                return "uv", inner[1:]  # Proven direct uv subcommand.
            if directories is not None:
                directory_options(arguments[: len(arguments) - len(inner)], directories)
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
            if directories is not None and executable == "sudo":
                directory_options(arguments[: len(arguments) - len(inner)], directories)
            tokens = inner
        elif executable == "env":
            env_directories: list[str] = []
            inner_env = unwrap_env_tokens(list(tokens), directories=env_directories)
            if inner_env is None:
                return None
            if directories is not None:
                for env_directory in env_directories:
                    directory_options(("--chdir", env_directory), directories)
            tokens = tuple(inner_env)
        elif executable == "rtk":
            if arguments[:1] in {("proxy",), ("bypass",)}:
                arguments = arguments[1:]
            if arguments[:1] == ("--",):
                arguments = arguments[1:]
            if arguments and arguments[0].startswith("-"):
                return None
            tokens = arguments
        elif executable in VALIDATION_LAUNCHER_HEADS:
            launcher_tokens = unwrap_validation_launcher_tokens([tokens[0], *arguments])
            if launcher_tokens is None:
                return None
            tokens = tuple(launcher_tokens)
        elif executable == "nice":
            return None  # Normalization already removed every proven nice prefix.
        else:
            return executable, arguments
    return None


def directory_options(arguments: tuple[str, ...], directories: list[str | None]) -> None:
    options = iter(arguments)
    for token in options:
        option, equals, value = token.partition("=")
        if option in {"--directory", "--chdir", "-D"}:
            directories.append(value if equals else next(options, None))
        elif token.startswith("-D"):
            directories.append(token[2:] or None)
        elif option in {"--chroot", "-R"} or token.startswith("-R"):
            directories.append(None)  # A different filesystem cannot prove repository ownership.
        elif (
            option
            in _UV_VALUE_OPTIONS
            | {
                "-u",
                "-g",
                "-h",
                "-p",
                "-C",
                "-T",
                "--user",
                "--group",
                "--host",
                "--prompt",
                "--close-from",
                "--command-timeout",
                "--preserve-env",
            }
            and not equals
        ):
            next(options, None)


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
