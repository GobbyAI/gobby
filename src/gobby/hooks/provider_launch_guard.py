"""Bounded inspection of literal provider execution in observed shell commands.

This is a workflow guard, not a shell interpreter or an OS security boundary.
Keep quote provenance until executable contexts have been identified.
"""

from __future__ import annotations

import re
from typing import Any

from gobby.hooks._normalization_shell import (
    ShellToken,
    _get_command_text,
    is_fd_duplication_token,
    is_shell_input_redirection_token,
    is_shell_output_redirection_token,
    is_shell_tool,
    scan_shell_command,
)

_PROVIDERS = frozenset({"codex", "claude", "droid", "grok", "qwen", "agy"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_MAX_DEPTH = 24
_MAX_LENGTH = 131_072


def blocks_direct_provider_launch(tool_name: Any, tool_input: Any) -> bool:
    """Rule predicate, independent of session variables and launch preferences."""
    command = _get_command_text(tool_input)
    return bool(is_shell_tool(tool_name) and command and _blocked(command, 0))


def _substitution_end(text: str, start: int, backtick: bool, depth: int) -> int:
    """Find the end of a literal substitution, respecting nested shell quotes."""
    if depth > _MAX_DEPTH:
        raise ValueError("Shell nesting limit")
    quote = ""
    level = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if backtick and char == "`":
            return index
        if quote == "'":
            if char == quote:
                quote = ""
        elif text.startswith("$(", index) or char == "`":
            tick = char == "`"
            index = _substitution_end(text, index + (1 if tick else 2), tick, depth + 1)
        elif char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif not quote and not backtick:
            if char == "(":
                level += 1
            elif char == ")":
                level -= 1
                if level == 0:
                    return index
        index += 1
    raise ValueError("Unclosed shell substitution")


def _prepare(command: str, depth: int, *, data: bool = False) -> tuple[str, list[str]]:
    """Mask expansions/comments and separate grouping for the shared shell scanner.

    Heredocs stay untouched for scan_shell_command to associate with their
    consumer. Unquoted data heredocs expand substitutions even inside quotes.
    """
    output: list[str] = []
    expansions: list[str] = []
    quote = ""
    index = 0
    line_start = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            output.append(command[index : index + 2])
            index += 2
            continue
        if quote != "'" and (
            command.startswith("$(", index)
            or char == "`"
            or (not data and not quote and command[index : index + 2] in {"<(", ">("})
        ):
            tick = char == "`"
            start = index + (1 if tick else 2)
            end = _substitution_end(command, start, tick, depth)
            expansions.append(command[start:end])
            output.append("__gobby_expansion__")
            index = end + 1
            continue
        if not data and char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif not data and not quote:
            if char == "#" and (index == 0 or command[index - 1] in " \t\n;|&()"):
                end = command.find("\n", index)
                index = len(command) if end < 0 else end
                continue
            if char in "()":
                char = ";"
            if char == "\n":
                # Only the current logical line is rescanned. A trailing pipe
                # delays heredoc consumption until its command is complete.
                line = scan_shell_command("".join(output[line_start:]))
                tokens = line.tokens
                if tokens and tokens[-1].value in {"|", "&&", "||"}:
                    output.append(" ")
                    index += 1
                    continue
                output.append(char)
                index += 1
                for offset, token in enumerate(tokens[:-1]):
                    if token.quoted or token.value not in {"<<", "<<-"}:
                        continue
                    delimiter = tokens[offset + 1].value
                    start = index
                    while index < len(command):
                        end = command.find("\n", index)
                        end = len(command) if end < 0 else end + 1
                        body_line = command[index:end].rstrip("\n")
                        index = end
                        if token.value == "<<-":
                            body_line = body_line.lstrip("\t")
                        if body_line == delimiter:
                            break
                    output.append(command[start:index])
                line_start = len(output)
                continue
        output.append(char)
        index += 1
    return "".join(output), expansions


def _executable_words(tokens: list[ShellToken]) -> list[str]:
    words: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif is_fd_duplication_token(token):
            continue
        elif is_shell_input_redirection_token(token) or is_shell_output_redirection_token(token):
            skip = True
        else:
            words.append(token.value)
    return words


def _separator(token: ShellToken) -> bool:
    return not token.quoted and token.value in {";", "|", "&", "&&", "||", "\n"}


def _unwrap(words: list[str]) -> list[str]:
    """Common literal execution wrappers; never treat query operands as launches."""
    while words:
        name = words[0].rsplit("/", 1)[-1]
        if _ASSIGNMENT.match(words[0]) or name in {
            "!",
            "{",
            "then",
            "do",
            "else",
            "elif",
            "if",
            "while",
            "until",
        }:
            words = words[1:]
        elif name in {
            "command",
            "builtin",
            "exec",
            "nohup",
            "time",
            "env",
            "nice",
            "timeout",
            "sudo",
            "setsid",
            "stdbuf",
            "xargs",
        }:
            words = words[1:]
            takes_value = {
                "env": {"-u", "--unset", "-C", "--chdir"},
                "exec": {"-a"},
                "nice": {"-n", "--adjustment"},
                "timeout": {"-s", "--signal", "-k", "--kill-after"},
                "sudo": {
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
                    "--chdir",
                },
                "time": {"-f", "-o", "--format", "--output"},
                "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
                "xargs": {
                    "-I",
                    "-J",
                    "-n",
                    "-P",
                    "-L",
                    "-s",
                    "-E",
                    "-d",
                    "--max-args",
                    "--max-procs",
                    "--delimiter",
                },
            }.get(name, set())
            while words and words[0].startswith("-"):
                option = words[0]
                if option == "--":
                    words = words[1:]
                    break
                if name == "command" and any(flag in option for flag in "vV"):
                    return []
                if name == "env" and option in {"-S", "--split-string"}:
                    # env -S interprets its argument as a command string.
                    return ["sh", "-c", " ".join(words[1:])]
                if name == "env" and option.startswith("--split-string="):
                    return ["sh", "-c", " ".join([option.split("=", 1)[1], *words[1:]])]
                words = words[2:] if option in takes_value else words[1:]
            if name == "timeout" and words:
                words = words[1:]
        elif name == "eval":
            return ["sh", "-c", " ".join(words[1:])]
        else:
            break
    return words


def _administration(args: list[str], provider: str) -> bool:
    # Only complete, literal forms: a help flag buried in a launch is not an exemption.
    return args in [["--help"], ["-h"], ["--version"]] or (
        (provider == "codex" and args == ["login", "status"])
        or (provider == "claude" and args == ["auth", "status"])
        or (provider == "codex" and args == ["-V"])
        or (provider in {"claude", "droid", "grok", "qwen"} and args == ["-v"])
        or (provider in {"codex", "droid", "grok", "agy"} and args == ["help"])
    )


def _shell_stdin(words: list[str]) -> bool:
    if not words or words[0].rsplit("/", 1)[-1] not in _SHELLS:
        return False
    args = iter(words[1:])
    for word in args:
        if word in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            next(args, None)
            continue
        if word.startswith("-"):
            if not word.startswith("--") and "c" in word:
                return False
            if not word.startswith("--") and "s" in word:
                return True
        else:
            return False
    return True


def _piped_to_shell(tokens: list[ShellToken], end: int) -> bool:
    while end < len(tokens) and tokens[end].value == "|":
        start = end + 1
        end = start
        while end < len(tokens) and not _separator(tokens[end]):
            end += 1
        if _shell_stdin(_unwrap(_executable_words(tokens[start:end]))):
            return True
    return False


def _blocked(command: str, depth: int) -> bool:
    if depth > _MAX_DEPTH or len(command) > _MAX_LENGTH:
        return True
    try:
        prepared, expansions = _prepare(command, depth)
        if any(_blocked(body, depth + 1) for body in expansions):
            return True
        scan = scan_shell_command(prepared)
        start = 0
        for end in range(len(scan.tokens) + 1):
            if end < len(scan.tokens) and not _separator(scan.tokens[end]):
                continue
            segment = scan.tokens[start:end]
            words = _unwrap(_executable_words(segment))
            bodies = [body for body in scan.heredocs if start <= body.opener < end]
            start = end + 1
            if not words:
                continue
            name = words[0].rsplit("/", 1)[-1]
            if name in _PROVIDERS and not _administration(words[1:], name):
                return True
            piped = _piped_to_shell(scan.tokens, end)
            if piped and name in {"echo", "printf"}:
                if any(_blocked(value, depth + 1) for value in words[1:]):
                    return True
            if name in _SHELLS:
                for index, arg in enumerate(words[1:], 1):
                    if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                        if index + 1 < len(words) and _blocked(words[index + 1], depth + 1):
                            return True
                        break
                # Literal here-strings are executable input as well.
                for index, token in enumerate(segment[:-1]):
                    if _shell_stdin(words) and not token.quoted and token.value == "<<<":
                        if _blocked(segment[index + 1].value, depth + 1):
                            return True
            if _shell_stdin(words) or piped:
                if any(_blocked(body.text, depth + 1) for body in bodies):
                    return True
            for body in bodies:
                if not body.quoted:
                    _, substitutions = _prepare(body.text, depth, data=True)
                    if any(_blocked(value, depth + 1) for value in substitutions):
                        return True
        return False
    except ValueError:
        # A malformed or over-deep command cannot earn an administrative exemption.
        return True
