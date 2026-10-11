"""Transparent wrapper grammars and command-local directory operands."""

import re

UNRESOLVED_DIRECTORY = "$GOBBY_UNRESOLVED_DIRECTORY"
UNRESOLVED_WRAPPER = "$GOBBY_UNRESOLVED_WRAPPER"
_DURATION = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)[smhd]?")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def env_command(parts: list[str]) -> tuple[list[str], tuple[str, ...]]:
    """Unwrap env; the last chdir wins within one invocation."""
    if not parts or parts[0].rsplit("/", 1)[-1] != "env":
        return parts, ()
    index = 1
    directories: tuple[str, ...] = ()
    remaining = list(parts)
    while index < len(remaining):
        word = remaining[index]
        if word == "--":
            index += 1
            while index < len(remaining) and "=" in remaining[index]:
                index += 1
            break
        if word in {"--help", "--version"}:
            return [], ()
        if word in {
            "-i",
            "--ignore-environment",
            "-",
            "-v",
            "--debug",
            "-0",
            "--null",
            "--list-signal-handling",
            "--block-signal",
            "--default-signal",
            "--ignore-signal",
        }:
            index += 1
        elif word.startswith(("--block-signal=", "--default-signal=", "--ignore-signal=")):
            index += 1
        elif word in {"-C", "--chdir", "-u", "--unset", "-a", "--argv0", "-P"}:
            if index + 1 >= len(remaining):
                return [UNRESOLVED_WRAPPER], ()
            value = remaining[index + 1]
            if word in {"-C", "--chdir"}:
                directories = (value,)
            index += 2
        elif word.startswith(("--chdir=", "-C", "--unset=", "-u", "--argv0=", "-a", "-P")):
            if word.startswith(("--chdir=", "-C")):
                directories = (word.split("=", 1)[1] if "=" in word else word[2:],)
            index += 1
        elif word.startswith("-"):
            return [UNRESOLVED_WRAPPER], ()
        elif "=" in word:
            index += 1
        else:
            break
    return remaining[index:], directories


def _utility_command(parts: list[str]) -> list[str]:
    """Unwrap nohup, timeout and nice using their option/operand boundaries."""
    if not parts:
        return parts
    name = parts[0].rsplit("/", 1)[-1]
    if name not in {"nohup", "timeout", "nice"}:
        return parts
    index = 1
    while index < len(parts):
        word = parts[index]
        if word == "--":
            index += 1
            break
        if word in {"--help", "--version"}:
            return []
        if name == "timeout" and word in {"--foreground", "--preserve-status", "--verbose", "-v"}:
            index += 1
        elif name == "timeout" and word in {"-s", "--signal", "-k", "--kill-after"}:
            if index + 1 >= len(parts):
                return [UNRESOLVED_WRAPPER]
            index += 2
        elif name == "timeout" and word.startswith(("--signal=", "--kill-after=", "-s", "-k")):
            index += 1
        elif name == "nice" and word in {"-n", "--adjustment"}:
            if index + 1 >= len(parts) or not re.fullmatch(r"[+-]?\d+", parts[index + 1]):
                return [UNRESOLVED_WRAPPER]
            index += 2
        elif name == "nice" and (
            re.fullmatch(r"[+-]\d+", word)
            or re.fullmatch(r"-n[+-]?\d+", word)
            or re.fullmatch(r"--adjustment=[+-]?\d+", word)
        ):
            index += 1
        elif word.startswith("-"):
            return [UNRESOLVED_WRAPPER]
        else:
            break
    if name == "timeout":
        if index >= len(parts) or not _DURATION.fullmatch(parts[index]):
            return [UNRESOLVED_WRAPPER]
        index += 1
    return parts[index:]


def _prefix(parts: list[str]) -> tuple[list[str], tuple[str, ...]]:
    if parts and (_ASSIGNMENT.match(parts[0]) or parts[0] in {"do", "then", "else"}):
        return parts[1:], ()
    if parts[:1] == ["command"]:
        index = 1
        while index < len(parts) and parts[index] in {"-p", "--"}:
            index += 1
        if index < len(parts) and not parts[index].startswith("-"):
            return parts[index:], ()
    command, directories = env_command(parts)
    return (_utility_command(parts), ()) if command == parts else (command, directories)


def strip_shell_wrappers(parts: list[str]) -> list[str]:
    remaining = parts
    while remaining:
        command, _ = _prefix(remaining)
        if command == remaining:
            break
        remaining = command
    return remaining


def env_directory_operands(parts: list[str]) -> tuple[str, ...]:
    """Collect env cwd operands through every supported transparent prefix."""
    directories: list[str] = []
    remaining = parts
    while remaining:
        command, changes = _prefix(remaining)
        if command == remaining:
            break
        directories.extend(changes)
        remaining = command
    return tuple(directories)
