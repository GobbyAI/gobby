"""Resolve literal shell programs without reading script files."""

from dataclasses import dataclass

from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS, _unwrap

SHELL_WRAPPER_DEPTH = 8


@dataclass(frozen=True)
class ShellExecution:
    command: str | None = None
    script_file: bool = False


def shell_execution(words: list[str]) -> ShellExecution | None:
    """Decode a shell's ``-c`` argument or identify opaque script execution.

    A script path is only an execution marker, never a content-authoring path.
    Option values and the positional parameters after ``-c`` are not programs.
    """
    words = _unwrap(words)
    if not words or shell_command_name(words[0]) not in _SHELLS:
        return None
    index = 1
    stdin_program = False
    while index < len(words):
        arg = words[index]
        if arg == "--":
            index += 1
            break
        if arg == "-":
            return ShellExecution()
        if not arg.startswith(("-", "+")):
            break
        if arg in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            index += 2
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            if "c" in arg:
                program = words[index + 1 : index + 3]
                if program[:1] == ["--"]:
                    program = program[1:]
                return ShellExecution(command=program[0]) if program else ShellExecution()
            stdin_program = stdin_program or "s" in arg
        index += 1
    return ShellExecution(script_file=not stdin_program and index < len(words))
