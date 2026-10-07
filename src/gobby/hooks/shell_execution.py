"""Resolve shell programs and bounded executable signatures without reading script bodies."""

import re
from dataclasses import dataclass
from pathlib import Path

from gobby.hooks._path_scope import resolve_tool_path
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS, _unwrap

SHELL_WRAPPER_DEPTH = 8
# ELF, then thin Mach-O (32/64-bit, both byte orders), then universal Mach-O (32/64-bit
# fat headers, both byte orders). When execve reports ENOEXEC, which a truncated or
# foreign binary also gets, a shell runs the file as a script unless its first line
# holds a NUL byte. Real headers reach a NUL within a few bytes.
_NATIVE_EXECUTABLE_MAGIC = frozenset(
    {
        b"\x7fELF",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)
# A command word that names one file before expansion: a slash, and no parameter,
# command, brace, glob, quote or function-definition syntax.
_STATIC_PATH = re.compile(r"[\w.~+-]*(?:/[\w.~+-]*)+")
# Wrappers whose options start the program elsewhere (env -C, sudo -D, sudo -i).
_DIRECTORY_WRAPPERS = frozenset({"env", "sudo"})


@dataclass(frozen=True)
class ShellExecution:
    command: str | None = None
    script_file: bool = False


def preserves_directory(words: list[str]) -> bool:
    """Whether a segment provably leaves this shell's directory unchanged.

    Only a static path runs a separate program. Any other command word may be a
    builtin, function or alias that changes directory, and a path followed by ``()``
    defines a function under that name.
    """
    words = _unwrap(words)
    return not words or (
        _STATIC_PATH.fullmatch(words[0]) is not None and words[1:2] not in (["()"], ["("])
    )


def starts_elsewhere(words: list[str]) -> bool:
    """Whether a wrapper may start the segment's program in another directory."""
    command = _unwrap(words)
    head = words[: words.index(command[0])] if command and command[0] in words else words
    return any(shell_command_name(word) in _DIRECTORY_WRAPPERS for word in head)


def path_invokes_script(words: list[str], base: Path | None) -> bool:
    """Gate path-invoked programs other than native executables; fail closed when unsure.

    ``base`` resolves a relative command word, and is ``None`` once the command may have
    changed directory. Only a 16-byte executable header is read, never the body.
    """
    words = _unwrap(words)
    if not words or "/" not in words[0]:
        return False
    try:
        path = resolve_tool_path(words[0], base)
        if path is None or not path.is_file():
            return True
        with path.open("rb") as executable:
            header = executable.read(16)
        first_line = header.split(b"\n", 1)[0]
        return header[:4] not in _NATIVE_EXECUTABLE_MAGIC or b"\x00" not in first_line
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
