"""Shell interpretation shared by remote-fetch safety analysis phases."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Protocol

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._normalization_shell import (
    ShellToken,
    _substitution_end,
    backtick_body,
    scan_shell_command,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._shell_execution_words import _unwrap, option_word_count
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.shell_execution import shell_execution
from gobby.workflows.engine.command_matching import _command_tokens


class _DescriptorHazard(Protocol):
    descriptor: bool


_FETCHERS = frozenset({"curl", "wget"})

_DATA_CONSUMERS = frozenset(
    {
        "echo",
        "printf",
        "[",
        "test",
        "jq",
        "cat",
        "diff",
        "tee",
        "grep",
        "egrep",
        "fgrep",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "base64",
        "sha256sum",
        "shasum",
        "md5sum",
        "md5",
        "xxd",
        "od",
    }
)

_DECLARATIONS = frozenset({"export", "local", "declare", "readonly", "typeset"})

_WRAPPERS = frozenset(
    {
        "env",
        "sudo",
        "command",
        "exec",
        "nohup",
        "nice",
        "timeout",
        "time",
        "xargs",
        "setsid",
        "doas",
        "busybox",
    }
)

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

_VARIABLE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([^}]*)\})")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_REDIRECTION = re.compile(r"^(\d*)(<<<|<<-|<<|<>|<|>>|>|>\||&>)$")

_INPUT_LIMIT = 64 * 1024

_DUP_OPERAND = re.compile(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\\.|[^\s;|&()\"'\\])+")


@dataclass(frozen=True)
class _Capture:
    body: str
    variable: bool = False
    arithmetic: bool = False
    output_sink: bool = False


def _possible_fetch(command: str) -> bool:
    # On malformed input the quote/escape context cannot establish an exemption.
    decoded = re.sub(r"[\\\"']", "", command)
    try:
        decoded += " " + " ".join(token.value for token in scan_shell_command(command).tokens)
    except (ValueError, RecursionError):
        pass  # Unknown quotes or bounds retain the conservative raw-text check.
    return bool(re.search(r"\b(?:curl|wget)\b", decoded))


def _capture_words(
    command: str, preflight: _DescriptorHazard | None = None
) -> tuple[str, dict[str, _Capture]]:
    """Replace expansions by opaque words while preserving each owning argument."""
    scan = scan_shell_command(command)
    heredocs = {body.start: body.end for body in scan.heredocs}
    prefix = "__gobby_fetch_capture_"
    while prefix in command:
        prefix += "_"
    captures: dict[str, _Capture] = {}
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        if index in heredocs:
            end = heredocs[index]
            out.append(command[index:end])
            index = end
            continue
        char = command[index]
        if char == "\\" and quote != "'":
            out.append(command[index : index + 2])
            index += 2
            continue
        if not quote and opens_ansi_c_quote(command, index):
            quote = "$"
            out.append("$'")
            index += 2
            continue
        if not quote and char == "#" and (index == 0 or command[index - 1] in " \t\n;|&("):
            end = command.find("\n", index)
            end = len(command) if end < 0 else end
            out.append(command[index:end])
            index = end
            continue
        if not quote:
            if preflight and (index == 0 or command[index - 1] in " \t\n;|&()"):
                opened = re.match(r"(\d+|\{[A-Za-z_]\w*\})[<>]", command[index:])
                if opened and (opened[1].startswith("{") or int(opened[1]) > 2):
                    preflight.descriptor = True
            if command[index : index + 2] in {"<&", ">&"}:
                start = index + 2
                while start < len(command) and command[start].isspace():
                    start += 1
                operand = _DUP_OPERAND.match(command, start)
                if operand is None:
                    raise ValueError("Unknown descriptor duplication")
                scanned = scan_shell_command(operand[0]).tokens
                value = scanned[0].value if len(scanned) == 1 else ""
                descriptor = value.removesuffix("-")
                literal = descriptor.isascii() and descriptor.isdigit()
                if preflight and value != "-" and (not literal or int(descriptor) > 2):
                    preflight.descriptor = True
                if literal:
                    explicit = re.search(r"(?:^|[\s;|&()])\d+$", command[:index])
                    destination = "1" if char == ">" and explicit is None else ""
                    out.append(f"{destination}< /dev/fd/{descriptor}")
                    index = operand.end()
                    continue
        opening = command.startswith("$(", index) or (
            not quote and command[index : index + 2] in {"<(", ">("}
        )
        if quote not in {"'", "$"} and command.startswith("$[", index):
            end = _arithmetic_end(command, index + 2)
            marker = f"{prefix}{len(captures)}__"
            captures[marker] = _Capture(command[index + 2 : end], arithmetic=True)
            out.append(marker)
            index = end + 1
            continue
        if quote not in {"'", "$"} and (opening or char == "`"):
            tick = char == "`"
            start = index + (1 if tick else 2)
            end = _substitution_end(command, start, tick, 1)
            body = command[start:end]
            marker = f"{prefix}{len(captures)}__"
            captures[marker] = _Capture(
                backtick_body(body) if tick else body,
                arithmetic=command.startswith("$((", index),
                output_sink=command.startswith(">(", index),
            )
            out.append(marker)
            index = end + 1
            continue
        if quote not in {"'", "$"} and char == "$" and (match := _VARIABLE.match(command, index)):
            marker = f"{prefix}{len(captures)}__"
            captures[marker] = _Capture(match[1] or match[2], variable=True)
            out.append(marker)
            index = match.end()
            continue
        if char in "\"'":
            if not quote:
                quote = char
            elif quote == char or (quote == "$" and char == "'"):
                quote = ""
        out.append(char)
        index += 1
    if quote:
        raise ValueError("Unbalanced shell quote")
    return "".join(out), captures


def _arithmetic_end(command: str, index: int) -> int:
    """Find the outer legacy arithmetic bracket without swallowing nested programs."""
    level = 1
    quote = ""
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if not quote and opens_ansi_c_quote(command, index):
            quote = "$'"
            index += 2
            continue
        if quote not in {"'", "$'"} and (command.startswith("$(", index) or char == "`"):
            tick = char == "`"
            index = _substitution_end(command, index + (1 if tick else 2), tick, 1) + 1
            continue
        if char in "\"'":
            if not quote:
                quote = char
            elif quote == char or (quote == "$'" and char == "'"):
                quote = ""
        elif not quote and char in "[]":
            level += 1 if char == "[" else -1
            if level == 0:
                return index
        index += 1
    raise ValueError("Unclosed shell arithmetic")


def _standard_word(word: str) -> bool:
    return posixpath.dirname(word) in {"", "/bin", "/usr/bin"}


def _execution_words(values: list[str]) -> list[str]:
    words = _unwrap(values)
    # An explicit unknown executable path cannot borrow a standard wrapper's identity.
    prefix = values[: len(values) - len(words)]
    if any(shell_command_name(word) in _WRAPPERS and not _standard_word(word) for word in prefix):
        return values
    return words


def _data_consumer(words: list[str]) -> bool:
    if not words or not _standard_word(words[0]):
        return False
    name = shell_command_name(words[0])
    if name == "printf":
        for word in words[1:]:
            if word == "--" or not word.startswith("-"):
                break
            if re.match(r"^-[a-zA-Z]*v", word):
                return False
    if name in {"python", "python3"}:
        return words[1:] == ["-m", "json.tool"]
    if name in {"tar", "unzip"}:
        if any(
            word.split("=", 1)[0]
            in {
                "--to-command",
                "-I",
                "--use-compress-program",
                "--checkpoint-action",
            }
            or re.match(r"-[a-zA-Z]*I", word)
            for word in words[1:]
        ):
            return False
        return name == "unzip" or any(
            word == "--extract" or "x" in word.lstrip("-") for word in words[1:2]
        )
    return name in _DATA_CONSUMERS


def _declaration_data(words: list[str]) -> bool:
    if (
        not words
        or not _standard_word(words[0])
        or shell_command_name(words[0]) not in _DECLARATIONS
    ):
        return False
    # Integer, array and nameref modes can interpret expansion output. Only
    # ordinary assignment attributes establish a data-capture exemption.
    return all(
        word == "--" or re.fullmatch(r"-[rxg]+", word)
        for word in words[1:]
        if word.startswith(("-", "+"))
    )


def _python_file_index(words: list[str]) -> int | None:
    if not words or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", shell_command_name(words[0])):
        return None
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-"):
            break
        if not (
            re.fullmatch(r"-[bBdEiIOPqRsSuvx]+", word)
            or word.startswith(("-W", "-X", "--check-hash-based-pycs"))
        ):
            return None
        index += option_word_count(word, {"-W", "-X", "--check-hash-based-pycs"})
    return index if index < len(words) and words[index] != "-" else None


def _data_argument(words: list[str], index: int) -> bool:
    if not words or index == 0 or not _standard_word(words[0]):
        return False
    name = shell_command_name(words[0])
    if _data_consumer(words):
        return True
    if name == "for" and words[2:3] == ["in"]:
        return index > 2
    execution = shell_execution(words)
    # A static script operand is the program; subsequent words are its data args.
    if execution and execution.script_word and execution.script_word in words:
        return index > words.index(execution.script_word)
    if (script_index := _python_file_index(words)) is not None:
        return index > script_index
    return False


def _file_key(word: str) -> str:
    path = posixpath.normpath(word)
    if path.startswith("//"):
        path = path[1:]
    if descriptor := re.fullmatch(r"/proc/[^/]+/fd/(\d+)", path):
        return f"/dev/fd/{descriptor[1]}"
    return path


def _output_files(tokens: list[ShellToken], words: list[str]) -> set[str]:
    paths = {
        _file_key(tokens[index + 1].value)
        for index, token in enumerate(tokens[:-1])
        if not token.quoted and token.value.lstrip("0123456789") in {">", ">>", ">|", "&>"}
    }
    if words and shell_command_name(words[0]) in _FETCHERS:
        options = (
            {"-o", "--output"}
            if shell_command_name(words[0]) == "curl"
            else {"-O", "--output-document"}
        )
        for index, word in enumerate(words[1:], 1):
            if word in options and index + 1 < len(words):
                paths.add(_file_key(words[index + 1]))
            for option in options:
                if word.startswith(option + "="):
                    paths.add(_file_key(word.partition("=")[2]))
                elif len(option) == 2 and word.startswith(option) and len(word) > 2:
                    paths.add(_file_key(word[2:]))
            output_flag = "o" if shell_command_name(words[0]) == "curl" else "O"
            if re.fullmatch(r"-[a-zA-Z]*" + output_flag, word):
                if index + 1 < len(words):
                    paths.add(_file_key(words[index + 1]))
    if words and _standard_word(words[0]) and shell_command_name(words[0]) == "tee":
        paths.update(_file_key(word) for word in words[1:] if not word.startswith("-"))
    return paths - {"-", "/dev/null"}


def _function_definition(tokens: list[ShellToken]) -> bool:
    if not tokens or tokens[0].quoted:
        return False
    word = tokens[0].value.removeprefix("=")
    return bool(
        word == "function"
        or word.startswith("()")
        or re.match(r"[A-Za-z_]\w*\(", word)
        or (
            len(tokens) > 1
            and _NAME.fullmatch(word)
            and not tokens[1].quoted
            and tokens[1].value.startswith("(")
        )
    )


def _stage_words(
    tokens: list[ShellToken], raw: dict[int, str]
) -> tuple[list[ShellToken], list[str], list[str]]:
    head = _command_tokens(strip_output_redirections(strip_input_redirections(tokens)))
    # Function definitions expose their body command without changing scanner tokens.
    if _function_definition(head):
        brace = next((index for index, token in enumerate(head[:4]) if token.value == "{"), None)
        if brace is not None:
            head = head[brace + 1 :]
    values = [token.value for token in head]
    # Resolve zsh EQUALS at each command position, including commands after wrappers.
    # Arguments and quoted command words retain their literal spelling.
    for _ in range(len(values)):
        words = _execution_words(values)
        if not words:
            break
        index = len(values) - len(words)
        if head[index].quoted or not values[index].startswith("="):
            break
        values[index] = values[index][1:]
        # Only one leading equals expands for this word; another is part of the name.
        if values[index].startswith("="):
            break
    offset = len(values) - len(_execution_words(values))
    protected = [
        "__gobby_literal_assignment__:" + value
        if index <= offset
        and _ASSIGNMENT.match(value)
        and not _ASSIGNMENT.match(raw[id(token)])
        and not any(word in {"env", "/bin/env", "/usr/bin/env"} for word in values[:index])
        else value
        for index, (token, value) in enumerate(zip(head, values, strict=True))
    ]
    return head, values, protected


def _interpreter(words: list[str]) -> bool:
    if not words:
        return False
    name = shell_command_name(words[0])
    return (
        shell_execution(words) is not None
        or name.startswith("python")
        or name
        in {
            "node",
            "perl",
            "ruby",
            "php",
            "eval",
            "source",
            ".",
        }
    )
