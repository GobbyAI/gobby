"""Resolve shell programs, executable signatures and bounded shell-script bodies."""

import re
from dataclasses import dataclass
from pathlib import Path

from gobby.hooks._path_scope import resolve_tool_path
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _SHELLS, _unwrap

SHELL_WRAPPER_DEPTH = 8
# Larger script bodies stay opaque.
SCRIPT_BODY_LIMIT = 64 * 1024
# Script bodies one command may read in all, since each body can run more scripts.
SCRIPT_READ_BUDGET = 8
# Room for a `#!` line: the kernel reads at most this much of it.
_HEADER_LIMIT = 512
# env options that take no value; any other option leaves the interpreter unknown.
_ENV_FLAGS = frozenset({"-", "-i", "-S", "-0", "-v", "--ignore-environment", "--debug"})
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
    # The word naming the file a shell reads its program from, when that file is
    # known; a script file without one stays opaque.
    script_word: str | None = None


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


def path_execution(words: list[str], base: Path | None) -> ShellExecution | None:
    """Classify a path-invoked program by its header; fail closed when unsure.

    ``None`` is a native executable or a script for a non-shell interpreter, which runs
    like ``python script.py``. A shell script (a shell ``#!`` line, or text a shell runs
    once execve reports ENOEXEC) names its resolved file in ``script_word``. ``base``
    resolves a relative command word, and is ``None`` once the command may have changed
    directory.
    """
    words = _unwrap(words)
    if not words or "/" not in words[0]:
        return None
    try:
        path = resolve_tool_path(words[0], base)
        if path is None or not path.is_file():
            return ShellExecution(script_file=True)
        with path.open("rb") as executable:
            header = executable.read(_HEADER_LIMIT)
    except (OSError, RuntimeError):
        return ShellExecution(script_file=True)
    first_line = header.split(b"\n", 1)[0]
    if header[:4] in _NATIVE_EXECUTABLE_MAGIC and b"\x00" in first_line:
        return None
    if first_line.startswith(b"#!"):
        interpreter = _shebang_interpreter(first_line[2:])
        if interpreter is None:
            return ShellExecution(script_file=True)
        if interpreter not in _SHELLS:
            return None
    return ShellExecution(script_file=True, script_word=str(path))


def _shebang_interpreter(line: bytes) -> str | None:
    """Name the program a ``#!`` line runs, looking through ``env``; ``None`` when unsure."""
    try:
        words = line.decode().split()
    except UnicodeDecodeError:
        return None
    if words and shell_command_name(words[0]) == "env":
        words = words[1:]
        while words and (words[0] in _ENV_FLAGS or ("=" in words[0] and words[0][0] != "-")):
            words = words[1:]
    if not words or words[0].startswith("-"):
        return None
    return shell_command_name(words[0])


def read_script_body(word: str, base: Path | None) -> tuple[Path, str] | None:
    """Read a whole shell script, or ``None`` when it is missing, unreadable, too large
    or not UTF-8."""
    try:
        path = resolve_tool_path(word, base)
        if path is None or not path.is_file():
            return None
        with path.open("rb") as script:
            body = script.read(SCRIPT_BODY_LIMIT + 1)
        if len(body) > SCRIPT_BODY_LIMIT:
            return None
        return path, body.decode()
    except (OSError, RuntimeError, UnicodeDecodeError):
        return None


def shell_execution(words: list[str], *, stdin: bool = False) -> ShellExecution | None:
    """Decode a shell's ``-c`` argument or identify script execution.

    A script operand names the file the shell reads its program from, never a
    content-authoring path. A program read from stdin, or by ``source`` in the calling
    shell, stays opaque. Option values and the positional parameters after ``-c`` are
    not programs.
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
    operand = index < len(words) and not stdin_program
    return ShellExecution(
        script_file=stdin_program or index < len(words) or stdin,
        script_word=words[index] if operand else None,
    )
