"""Resolve shell programs and bounded executable signatures without reading script bodies."""

from dataclasses import dataclass
from pathlib import Path

from gobby.hooks._path_scope import resolve_tool_path
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS, _unwrap

SHELL_WRAPPER_DEPTH = 8


@dataclass(frozen=True)
class ShellExecution:
    command: str | None = None
    script_file: bool = False


def path_invokes_script(words: list[str], cwd: str | None, base: Path | None) -> bool:
    """Gate path-invoked shebang scripts; unresolvable or unreadable paths fail closed.

    ``cwd`` is the command's own ``cd`` state and ``base`` the tool call's working
    directory. Only the two-byte executable signature is read, never the program body.
    """
    words = _unwrap(words)
    if not words or "/" not in words[0]:
        return False
    try:
        path = resolve_tool_path(words[0], resolve_tool_path(cwd, base) if cwd else base)
        if path is None or not path.is_file():
            return True
        with path.open("rb") as executable:
            return executable.read(2) == b"#!"
    except (OSError, RuntimeError):
        return True


def shell_execution(words: list[str], *, stdin: bool = False) -> ShellExecution | None:
    """Decode a shell's ``-c`` argument or identify opaque script execution.

    A script path is only an execution marker, never a content-authoring path.
    Option values and the positional parameters after ``-c`` are not programs.
    """
    words = _unwrap(words)
    if words and shell_command_name(words[0]) in {"source", "."}:
        return ShellExecution(script_file=len(words) > 1)
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
            return ShellExecution(script_file=True)
        if not arg.startswith(("-", "+")):
            break
        if arg in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            index += 2
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            stdin_program = stdin_program or "s" in arg
        if not arg.startswith(("--", "++")) and ("o" in arg or "O" in arg):
            index += 2
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            if "c" in arg:
                program = words[index + 1 : index + 3]
                if program[:1] == ["--"]:
                    program = program[1:]
                return ShellExecution(command=program[0]) if program else ShellExecution()
        index += 1
    return ShellExecution(script_file=stdin_program or index < len(words) or stdin)
