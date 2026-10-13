"""Reject fetched shell output unless its individual use is proven to be data."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gobby.hooks._normalization_shell import ShellToken, scan_shell_command
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH
from gobby.workflows.engine.command_matching import (
    _pipeline_stages,
    _shell_input_scripts,
    _split_segments,
    _wrapper_scripts,
)
from gobby.workflows.remote_fetch_preflight import _command_preflight, _Preflight
from gobby.workflows.remote_fetch_shell import (
    _ASSIGNMENT,
    _DECLARATIONS,
    _FETCHERS,
    _INPUT_LIMIT,
    _NAME,
    _REDIRECTION,
    _Capture,
    _capture_words,
    _data_argument,
    _data_consumer,
    _declaration_data,
    _execution_words,
    _file_key,
    _interpreter,
    _output_files,
    _possible_fetch,
    _stage_words,
)


@dataclass
class _State:
    files: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)
    exported: set[str] = field(default_factory=set)
    restricted: set[str] = field(default_factory=set)
    fds: set[str] = field(default_factory=set)

    def fork(self) -> _State:
        return _State(
            self.files,
            self.variables.copy(),
            self.exported.copy(),
            self.restricted.copy(),
            self.fds.copy(),
        )


@dataclass(frozen=True)
class _Result:
    output: bool = False
    blocked: bool = False


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
    preflight = _Preflight()
    try:
        _command_preflight(command, preflight, 0)
        if preflight.fetch and (preflight.deferred or preflight.descriptor):
            return True
        return _analyze(command, _State(), 0).blocked
    except (ValueError, RecursionError):
        return preflight.fetch or _possible_fetch(command)
