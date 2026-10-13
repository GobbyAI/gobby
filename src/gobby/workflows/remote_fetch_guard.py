"""Reject fetched shell output unless its individual use is proven to be data.

Unknown consumers are executable by default. This is a static, bounded analysis;
it neither runs commands nor reads script files. The shell scanner and wrapper
decoder share the command selector's quoting and command-position semantics.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._normalization_shell import (
    ShellToken,
    _substitution_end,
    backtick_body,
    scan_shell_command,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH, shell_execution
from gobby.workflows.engine.command_matching import (
    _command_tokens,
    _pipeline_stages,
    _shell_input_scripts,
    _split_segments,
    _wrapper_scripts,
)

_FETCHERS = frozenset({"curl", "wget"})
_DATA_CONSUMERS = frozenset({"echo", "printf", "[", "test", "jq", "cat", "diff", "tee"})
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
_VARIABLE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
_INPUT_LIMIT = 64 * 1024


@dataclass
class _State:
    files: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Capture:
    body: str
    variable: bool = False


@dataclass(frozen=True)
class _Result:
    output: bool = False
    blocked: bool = False


def _possible_fetch(command: str) -> bool:
    # On malformed input the quote/escape context cannot establish an exemption.
    decoded = re.sub(r"[\\\"']", "", command)
    return bool(re.search(r"\b(?:curl|wget)\b", decoded))


def _capture_words(command: str) -> tuple[str, dict[str, _Capture]]:
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
        opening = command.startswith("$(", index) or (
            not quote and command[index : index + 2] in {"<(", ">("}
        )
        if quote not in {"'", "$"} and (opening or char == "`"):
            tick = char == "`"
            start = index + (1 if tick else 2)
            end = _substitution_end(command, start, tick, 1)
            body = command[start:end]
            marker = f"{prefix}{len(captures)}__"
            captures[marker] = _Capture(backtick_body(body) if tick else body)
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
    return bool(
        words and _standard_word(words[0]) and shell_command_name(words[0]) in _DATA_CONSUMERS
    )


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


def _data_argument(words: list[str], index: int) -> bool:
    if not words or index == 0 or not _standard_word(words[0]):
        return False
    name = shell_command_name(words[0])
    if name in _DATA_CONSUMERS:
        return True
    if name == "for" and words[2:3] == ["in"]:
        return index > 2
    execution = shell_execution(words)
    # A static script operand is the program; subsequent words are its data args.
    if execution and execution.script_word and execution.script_word in words:
        return index > words.index(execution.script_word)
    return False


def _file_key(word: str) -> str:
    return posixpath.normpath(word)


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
    return paths - {"-", "/dev/null"}


def _stage_words(
    tokens: list[ShellToken], raw: dict[int, str]
) -> tuple[list[ShellToken], list[str], list[str]]:
    head = _command_tokens(strip_output_redirections(strip_input_redirections(tokens)))
    values = [token.value for token in head]
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


def _stage(
    tokens: list[ShellToken],
    captures: dict[str, _Capture],
    state: _State,
    depth: int,
    raw: dict[int, str],
) -> _Result:
    head, values, protected = _stage_words(tokens, raw)
    words = _execution_words(protected)
    offset = len(values) - len(words)
    name = shell_command_name(words[0]) if words else ""
    remote = name in _FETCHERS
    command_variables = state.variables.copy()
    for index, value in enumerate(values):
        assignment = _ASSIGNMENT.match(value)
        assignment_data = bool(
            assignment
            and (
                (
                    index < offset
                    and (
                        _ASSIGNMENT.match(raw[id(head[index])])
                        or any(
                            word in {"env", "/bin/env", "/usr/bin/env"}
                            for word in protected[:index]
                        )
                    )
                )
                or _declaration_data(words)
            )
        )
        token_remote = False
        for marker, capture in captures.items():
            if marker not in value:
                continue
            if capture.variable:
                result = _Result(capture.body in state.variables)
            else:
                result = _analyze(
                    capture.body, _State(state.files, state.variables.copy()), depth + 1
                )
            if result.blocked:
                return result
            if not result.output:
                continue
            token_remote = True
            if not assignment_data and not _data_argument(words, index - offset):
                return _Result(blocked=True)
        if assignment_data and assignment:
            variables = (
                state.variables if not words or _declaration_data(words) else command_variables
            )
            if token_remote:
                variables.add(assignment[1])
            else:
                variables.discard(assignment[1])
        elif token_remote:
            remote = True
            if name == "for" and len(words) > 1:
                state.variables.add(words[1])

    # A file written by a fetch (or a data consumer printing fetch output) can
    # subsequently become a program. Unknown consumers are not data exemptions.
    input_files = {
        _file_key(tokens[index + 1].value)
        for index, token in enumerate(tokens[:-1])
        if not token.quoted and token.value.lstrip("0123456789") == "<"
    }
    if state.files.intersection({_file_key(word) for word in values[offset:]} | input_files):
        if not _data_consumer(words):
            return _Result(blocked=True)
        remote = True
    for script in _wrapper_scripts([protected]):
        result = _analyze(script, _State(state.files, command_variables), depth + 1)
        if result.blocked:
            return result
        remote |= result.output
    if remote:
        state.files.update(_output_files(tokens, words))
    return _Result(remote and name not in {*_DECLARATIONS, "for"})


def _heredoc_result(
    text: str, quoted: bool, words: list[str], state: _State, depth: int
) -> _Result:
    if not _data_consumer(words):
        return _analyze(text, _State(state.files, state.variables.copy()), depth + 1)
    if quoted:
        return _Result()
    _, captures = _capture_words(text)
    remote = False
    for capture in captures.values():
        if capture.variable:
            result = _Result(capture.body in state.variables)
        else:
            result = _analyze(capture.body, _State(state.files, state.variables.copy()), depth + 1)
        if result.blocked:
            return result
        remote |= result.output
    return _Result(remote)


def _analyze(command: str, state: _State, depth: int) -> _Result:
    if depth > SHELL_WRAPPER_DEPTH or len(command) > _INPUT_LIMIT:
        return _Result(blocked=_possible_fetch(command) or bool(state.variables))
    rendered, captures = _capture_words(command)
    scan = scan_shell_command(rendered)
    raw = {
        id(token): rendered[start:end]
        for token, (start, end) in zip(scan.tokens, scan.spans, strict=True)
    }
    remote_output = False
    for segment in _split_segments(scan.tokens):
        tokens = scan.tokens[segment.first : segment.last + 1]
        pipeline_remote = False
        for stage in _pipeline_stages(tokens):
            _, _, protected = _stage_words(stage, raw)
            words = _execution_words(protected)
            if pipeline_remote and not _data_consumer(words):
                return _Result(blocked=True)
            heredoc_remote = False
            for heredoc in scan.heredocs:
                if not any(token is scan.tokens[heredoc.opener] for token in stage):
                    continue
                result = _heredoc_result(heredoc.text, heredoc.quoted, words, state, depth)
                if result.blocked:
                    return result
                heredoc_remote |= result.output
            result = _stage(stage, captures, state, depth, raw)
            if result.blocked:
                return result
            pipeline_remote |= result.output or heredoc_remote
            if pipeline_remote:
                state.files.update(_output_files(stage, words))
        remote_output |= pipeline_remote
    for script in _shell_input_scripts(rendered):
        result = _analyze(script, _State(state.files, state.variables.copy()), depth + 1)
        if result.blocked:
            return result
        remote_output |= result.output
    return _Result(remote_output)


def remote_fetch_execution(command: object) -> bool:
    """Whether remote output reaches an executable use in this shell command."""
    if not isinstance(command, str) or not command:
        return False
    try:
        return _analyze(command, _State(), 0).blocked
    except (ValueError, RecursionError):
        return _possible_fetch(command)
