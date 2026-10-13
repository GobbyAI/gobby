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
from gobby.hooks._shell_execution_words import _unwrap, option_word_count
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


@dataclass
class _State:
    files: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)
    exported: set[str] = field(default_factory=set)
    restricted: set[str] = field(default_factory=set)
    fds: set[str] = field(default_factory=set)
    allexport: bool = False

    def fork(self) -> _State:
        return _State(
            self.files,
            self.variables.copy(),
            self.exported.copy(),
            self.restricted.copy(),
            self.fds.copy(),
            self.allexport,
        )


@dataclass(frozen=True)
class _Capture:
    body: str
    variable: bool = False
    arithmetic: bool = False
    output_sink: bool = False


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
        # Keep fd duplication in one input redirection before the scanner splits '&'.
        if not quote and (duplicate := re.match(r"<&\s*(\d+)-?(?=$|[\s;|&])", command[index:])):
            out.append(f"< /dev/fd/{duplicate[1]}")
            index += duplicate.end()
            continue
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
        if quote not in {"'", "$"} and command.startswith("$[", index):
            end = command.find("]", index + 2)
            if end < 0:
                raise ValueError("Unclosed shell arithmetic")
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
            output_flag = "o" if shell_command_name(words[0]) == "curl" else "O"
            if re.fullmatch(r"-[a-zA-Z]*" + output_flag, word):
                if index + 1 < len(words):
                    paths.add(_file_key(words[index + 1]))
    if words and _standard_word(words[0]) and shell_command_name(words[0]) == "tee":
        paths.update(_file_key(word) for word in words[1:] if not word.startswith("-"))
    return paths - {"-", "/dev/null"}


def _stage_words(
    tokens: list[ShellToken], raw: dict[int, str]
) -> tuple[list[ShellToken], list[str], list[str]]:
    head = _command_tokens(strip_output_redirections(strip_input_redirections(tokens)))
    # Function definitions expose their body command without changing scanner tokens.
    if (
        head
        and not head[0].quoted
        and (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)", head[0].value)
            or head[0].value == "function"
        )
    ):
        brace = next((index for index, token in enumerate(head[:4]) if token.value == "{"), None)
        if brace is not None:
            head = head[brace + 1 :]
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


def _capture_result(capture: _Capture, state: _State, depth: int) -> _Result:
    if capture.variable:
        if _NAME.fullmatch(capture.body):
            return _Result(capture.body in state.variables)
        if depth > SHELL_WRAPPER_DEPTH:
            return _Result(blocked=_possible_fetch(capture.body) or bool(state.variables))
        _, nested = _capture_words(capture.body)
        remote_output = False
        for child in nested.values():
            result = _capture_result(child, state, depth + 1)
            if result.blocked:
                return result
            remote_output |= result.output
        names = set(_NAME.findall(capture.body))
        return _Result(
            remote_output,
            blocked=bool(names & state.variables)
            or (capture.body.startswith(("!", "(")) and bool(state.variables)),
        )
    result = _analyze(capture.body, state.fork(), depth + 1)
    if capture.arithmetic and (result.output or set(_NAME.findall(capture.body)) & state.variables):
        return _Result(blocked=True)
    return result


def _restricted_names(words: list[str]) -> set[str]:
    if not words or words[0] not in _DECLARATIONS:
        return set()
    if not any(re.fullmatch(r"-[a-zA-Z]*[inaA][a-zA-Z]*", word) for word in words[1:]):
        return set()
    return {word.split("=", 1)[0] for word in words[1:] if _NAME.fullmatch(word.split("=", 1)[0])}


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


def _stage(
    tokens: list[ShellToken],
    captures: dict[str, _Capture],
    state: _State,
    depth: int,
    raw: dict[int, str],
    stdin_remote: bool = False,
) -> _Result:
    head, values, protected = _stage_words(tokens, raw)
    words = _execution_words(protected)
    offset = len(values) - len(words)
    name = shell_command_name(words[0]) if words else ""
    remote = name in _FETCHERS
    if name == "set":
        for index, word in enumerate(words[1:], 1):
            if word in {"-o", "+o"}:
                if words[index + 1 : index + 2] == ["allexport"]:
                    state.allexport = word == "-o"
            elif re.fullmatch(r"[-+][a-zA-Z]+", word) and "a" in word:
                state.allexport = word.startswith("-")
            elif word == "--":
                break
    if (
        state.variables
        and any(word in {"eval", "source", "."} for word in protected[: max(offset + 1, 1)])
        and any(r"\$" in raw[id(token)] for token in head)
    ):
        return _Result(blocked=True)
    if name in {"declare", "typeset", "local"} and any(
        re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", word) for word in words[1:]
    ):
        for word in words[1:]:
            alias, separator, target = word.partition("=")
            if separator and _NAME.fullmatch(alias) and target in state.variables:
                state.variables.add(alias)
    if name == "export" or (
        name in _DECLARATIONS and any(word.startswith("-") and "x" in word for word in words[1:])
    ):
        state.exported.update(
            word.split("=", 1)[0] for word in words[1:] if _NAME.fullmatch(word.split("=", 1)[0])
        )
    command_variables = state.variables.copy()
    command_exported = state.exported.copy()
    results = {
        marker: _capture_result(capture, state, depth)
        for marker, capture in captures.items()
        if any(marker in token.value for token in tokens)
    }
    if any(result.blocked for result in results.values()):
        return _Result(blocked=True)

    # Every redirection operand is evaluated, even when removed from executable argv.
    # Remote stdin follows descriptors as well as pipes; named fd opens are not stdin.
    for index, token in enumerate(tokens):
        if token.quoted:
            continue
        duplication = token.value
        if re.fullmatch(r"\d*<&", duplication) and index + 1 < len(tokens):
            duplication += tokens[index + 1].value
        if duplicate := re.fullmatch(r"(\d*)<&(\d+)-?", duplication):
            if duplicate[2] in state.fds:
                descriptor = duplicate[1] or "0"
                state.fds.add(descriptor)
                state.files.add(f"/dev/fd/{descriptor}")
                if descriptor == "0":
                    state.files.add("/dev/stdin")
                stdin_remote |= not duplicate[1] or duplicate[1] == "0"
        redirection = _REDIRECTION.fullmatch(token.value)
        if not redirection or index + 1 == len(tokens):
            continue
        operand = tokens[index + 1].value
        supplied = any(result.output for marker, result in results.items() if marker in operand)
        supplied |= _file_key(operand) in state.files
        if redirection[2] in {"<", "<<<", "<>"} and supplied:
            descriptor = redirection[1] or "0"
            state.fds.add(descriptor)
            state.files.add(f"/dev/fd/{descriptor}")
            if descriptor == "0":
                state.files.add("/dev/stdin")
            stdin_remote |= not redirection[1] or redirection[1] == "0"
    if stdin_remote:
        if not _data_consumer(words):
            return _Result(blocked=True)
        remote = True

    arithmetic = (
        name == "let"
        or (
            name == "[["
            and any(word in {"-eq", "-ne", "-lt", "-le", "-gt", "-ge"} for word in words)
        )
        or any(
            not token.quoted
            and (
                token.value.startswith("((")
                or (
                    token.value.startswith("[[")
                    and re.search(r"\s-(?:eq|ne|lt|le|gt|ge)\s", token.value)
                )
                or re.search(r"[A-Za-z_]\w*\[", token.value)
            )
            for token in head
        )
    )
    if arithmetic and set(_NAME.findall(" ".join(values))) & state.variables:
        return _Result(blocked=True)

    for index, value in enumerate(values):
        if not head[index].quoted and value.startswith("(") and value.endswith(")"):
            result = _analyze(value[1:-1], state.fork(), depth + 1)
            if result.blocked:
                return result
            remote |= result.output
        assignment = _ASSIGNMENT.match(value)
        if (
            assignment
            and assignment[1] in {"BASH_ENV", "ENV"}
            and index < offset
            and _interpreter(words)
            and (_file_key(value[assignment.end() :]) in state.files)
        ):
            return _Result(blocked=True)
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
        for marker, result in results.items():
            if marker not in value:
                continue
            if captures[marker].output_sink:
                continue
            if not result.output:
                continue
            token_remote = True
            if assignment and (
                value[assignment.end() :].startswith("(") or assignment[1] in state.restricted
            ):
                return _Result(blocked=True)
            if not assignment_data and not _data_argument(words, index - offset):
                return _Result(blocked=True)
        if assignment_data and assignment:
            variables = (
                state.variables if not words or _declaration_data(words) else command_variables
            )
            if token_remote:
                variables.add(assignment[1])
                if state.allexport:
                    state.exported.add(assignment[1])
                    command_exported.add(assignment[1])
                if index < offset:
                    command_exported.add(assignment[1])
                elif name == "export" or (
                    name in _DECLARATIONS
                    and any(word.startswith("-") and "x" in word for word in words[1:])
                ):
                    state.exported.add(assignment[1])
                    command_exported.add(assignment[1])
        elif token_remote:
            remote = True
            if name == "for" and len(words) > 1:
                state.variables.add(words[1])

    # Taint is monotone: conditional assignments cannot prove an earlier fetch was erased.
    if state.variables & state.restricted:
        return _Result(blocked=True)
    exposed = command_exported & (state.variables | command_variables)
    if (
        exposed
        and name not in _DECLARATIONS
        and not _data_consumer(words)
        and (_interpreter(words) or exposed & set(_NAME.findall(" ".join(words))))
    ):
        return _Result(blocked=True)

    # A file written by a fetch (or a data consumer printing fetch output) can
    # subsequently become a program. Unknown consumers are not data exemptions.
    input_files = {
        _file_key(tokens[index + 1].value)
        for index, token in enumerate(tokens[:-1])
        if not token.quoted and token.value.lstrip("0123456789") == "<"
    }
    file_arguments = [index for index, word in enumerate(values) if _file_key(word) in state.files]
    if file_arguments or state.files.intersection(input_files):
        if any(not _data_argument(words, index - offset) for index in file_arguments) or (
            state.files.intersection(input_files) and not _data_consumer(words)
        ):
            return _Result(blocked=True)
        remote = True
    for marker, capture in captures.items():
        if marker in results and capture.output_sink and remote:
            result = _analyze(capture.body, state.fork(), depth + 1, stdin_remote=True)
            if result.blocked:
                return result
    for script in _wrapper_scripts([protected]):
        child = state.fork()
        child.variables = command_variables
        child.exported = command_exported
        # eval executes in the caller; its assignments remain visible afterwards.
        if "eval" in protected[: max(offset + 2, 1)]:
            child = state
        result = _analyze(script, child, depth + 1)
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
        return _analyze(text, state.fork(), depth + 1)
    if quoted:
        return _Result()
    _, captures = _capture_words(text)
    remote = False
    for capture in captures.values():
        result = _capture_result(capture, state, depth)
        if result.blocked:
            return result
        remote |= result.output
    return _Result(remote)


def _analyze(command: str, state: _State, depth: int, *, stdin_remote: bool = False) -> _Result:
    if depth > SHELL_WRAPPER_DEPTH or len(command) > _INPUT_LIMIT:
        return _Result(blocked=_possible_fetch(command) or bool(state.variables))
    rendered, captures = _capture_words(command)
    scan = scan_shell_command(rendered)
    raw = {
        id(token): rendered[start:end]
        for token, (start, end) in zip(scan.tokens, scan.spans, strict=True)
    }
    for segment in _split_segments(scan.tokens):
        for stage in _pipeline_stages(scan.tokens[segment.first : segment.last + 1]):
            _, _, protected = _stage_words(stage, raw)
            state.restricted.update(_restricted_names(_execution_words(protected)))
    remote_output = False
    for segment in _split_segments(scan.tokens):
        tokens = scan.tokens[segment.first : segment.last + 1]
        pipeline_remote = stdin_remote
        for stage in _pipeline_stages(tokens):
            _, _, protected = _stage_words(stage, raw)
            words = _execution_words(protected)
            heredoc_remote = False
            for heredoc in scan.heredocs:
                if not any(token is scan.tokens[heredoc.opener] for token in stage):
                    continue
                result = _heredoc_result(heredoc.text, heredoc.quoted, words, state, depth)
                if result.blocked:
                    return result
                heredoc_remote |= result.output
            result = _stage(stage, captures, state, depth, raw, pipeline_remote or heredoc_remote)
            if result.blocked:
                return result
            pipeline_remote |= result.output or heredoc_remote
            if pipeline_remote:
                state.files.update(_output_files(stage, words))
        remote_output |= pipeline_remote
    for script in _shell_input_scripts(rendered):
        result = _analyze(script, state.fork(), depth + 1)
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
