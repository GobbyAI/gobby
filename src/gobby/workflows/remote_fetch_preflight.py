"""Whole-input remote-fetch hazards discovered before data exemptions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._normalization_shell import (
    ShellToken,
    scan_shell_command,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH, shell_execution
from gobby.workflows.engine.command_matching import (
    _command_tokens,
    _pipeline_stages,
    _shell_input_scripts,
    _split_segments,
    _wrapper_scripts,
)
from gobby.workflows.remote_fetch_shell import (
    _DECLARATIONS,
    _FETCHERS,
    _INPUT_LIMIT,
    _Capture,
    _capture_words,
    _data_consumer,
    _execution_words,
    _file_key,
    _function_definition,
    _stage_words,
)


@dataclass
class _Preflight:
    fetch: bool = False
    deferred: bool = False
    descriptor: bool = False
    seen: set[str] = field(default_factory=set)


def _allexport_toggle(words: list[str], captures: dict[str, _Capture]) -> bool:
    def option(word: str) -> bool:
        if any(marker in word for marker in captures):
            raise ValueError("Unknown allexport option")
        return word.lower().replace("_", "").removeprefix("no") == "allexport"

    if words[0] in {"setopt", "unsetopt"}:
        return any(option(word) for word in words[1:])
    if words[0] != "set":
        return False
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            break
        if any(marker in word for marker in captures):
            raise ValueError("Unknown set option")
        if not re.fullmatch(r"[-+][a-zA-Z]+", word):
            break
        if "a" in word:
            return True
        if word.endswith("o") and index + 1 < len(words):
            if option(words[index + 1]):
                return True
            index += 1
        index += 1
    return False


def _shell_c_wrapper(words: list[str], captures: dict[str, _Capture]) -> bool:
    execution = shell_execution(words)
    if execution is None:
        raise ValueError("Unknown shell wrapper")
    if execution.command is not None:
        return True
    end = (
        words.index(execution.script_word)
        if execution.script_word is not None and execution.script_word in words
        else len(words)
    )
    if any(marker in word for word in words[1 : end + 1] for marker in captures):
        raise ValueError("Unknown shell options")
    if any(re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word) for word in words[1:end]):
        raise ValueError("Unknown shell command body")
    return False


def _descriptor_path(word: str, captures: dict[str, _Capture]) -> bool:
    path = _file_key(word)
    if not path.startswith("/"):
        return False
    path = "/" + path.lstrip("/")
    match = re.match(r"^/(?:dev/fd|proc/[^/]+/fd)/(.*)$", path)
    return bool(
        match
        and (
            (match[1].isascii() and match[1].isdigit() and int(match[1]) > 2)
            or any(marker in match[1] for marker in captures)
            or "$" in match[1]
            or "`" in match[1]
        )
    )


def _preflight_capture(capture: _Capture, features: _Preflight, depth: int) -> None:
    if depth > SHELL_WRAPPER_DEPTH:
        raise ValueError("Unknown nested command")
    if capture.variable or capture.arithmetic:
        # Parameter/arithmetic text is data; its executable substitutions are commands.
        _, children = _capture_words(capture.body)
        for child in children.values():
            _preflight_capture(child, features, depth + 1)
    else:
        _command_preflight(capture.body, features, depth + 1)


def _preflight_segments(tokens: list[ShellToken]) -> list[list[ShellToken]]:
    """Expose case branch bodies without interpreting their pattern words as commands."""
    cases: list[bool] = []
    bodies: list[list[ShellToken]] = []
    for segment in _split_segments(tokens):
        head = tokens[segment.first : segment.last + 1]
        while head:
            head = _command_tokens(head)
            if not head:
                break
            if not head[0].quoted and head[0].value == "case":
                marker = next(
                    (
                        i
                        for i, token in enumerate(head[1:], 1)
                        if not token.quoted and token.value == "in"
                    ),
                    None,
                )
                if marker is None:
                    raise ValueError("Unknown case header")
                cases.append(True)
                head = head[marker + 1 :]
            elif not head[0].quoted and head[0].value == "esac":
                if not cases:
                    raise ValueError("Unknown case boundary")
                cases.pop()
                head = head[1:]
            elif cases and cases[-1]:
                closing = next(
                    (
                        i
                        for i, token in enumerate(head)
                        if not token.quoted and token.value.endswith(")")
                    ),
                    None,
                )
                if closing is None:
                    head = []
                else:
                    head = head[closing + 1 :]
                    cases[-1] = False
            else:
                break
        if head:
            bodies.append(head)
        separators = [token.value for token in tokens[segment.last + 1 : segment.last + 4]]
        if cases and len(separators) > 1 and separators[0] == ";" and separators[1] in {";", "&"}:
            cases[-1] = True
    if cases:
        raise ValueError("Unclosed case statement")
    return bodies


def _dynamic_command_word(word: str) -> bool:
    """Recognize raw expansions without treating literal quoted dollars as commands."""
    quote = ""
    index = 0
    while index < len(word):
        char = word[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if not quote and opens_ansi_c_quote(word, index):
            quote = "$'"
            index += 2
            continue
        if char in "\"'" and (not quote or quote == char):
            quote = char if not quote else ""
        elif quote == "$'" and char == "'":
            quote = ""
        elif quote not in {"'", "$'"} and char in {"$", "`"}:
            return True
        index += 1
    return False


def _command_preflight(command: str, features: _Preflight, depth: int) -> None:
    """Discover complete-input fetches and hazards before deciding any exemption."""
    if depth > SHELL_WRAPPER_DEPTH or len(command) > _INPUT_LIMIT:
        raise ValueError("Unknown command bounds")
    if command in features.seen:
        return
    features.seen.add(command)
    rendered, captures = _capture_words(command, features)
    for capture in captures.values():
        _preflight_capture(capture, features, depth)
    scan = scan_shell_command(rendered)
    raw = {
        id(token): rendered[start:end]
        for token, (start, end) in zip(scan.tokens, scan.spans, strict=True)
    }
    scripts = set(_shell_input_scripts(rendered))
    for body in _preflight_segments(scan.tokens):
        for stage in _pipeline_stages(body):
            original = _command_tokens(strip_output_redirections(strip_input_redirections(stage)))
            if _function_definition(original, raw):
                features.deferred = True
            head, values, protected = _stage_words(stage, raw)
            words = _execution_words(protected)
            offset = len(values) - len(words)
            name = shell_command_name(words[0]) if words else ""
            for index, value in enumerate(values):
                if value.startswith("SHELLOPTS=") and (index < offset or name in _DECLARATIONS):
                    if any(marker in value for marker in captures):
                        raise ValueError("Unknown SHELLOPTS assignment")
                    features.deferred |= any(
                        item.lower().replace("_", "") == "allexport"
                        for item in value.partition("=")[2].split(":")
                    )
            if words:
                features.fetch |= name in _FETCHERS
                features.deferred |= (
                    any(
                        word in {"trap", "eval", "source", ".", "alias"}
                        for word in protected[: offset + 1]
                    )
                    or any(marker in words[0] for marker in captures)
                    or _dynamic_command_word(raw[id(head[offset])])
                    or (
                        name in {"declare", "typeset", "local"}
                        and any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", word) for word in words[1:])
                    )
                    or _allexport_toggle(words, captures)
                    or (
                        name in {"sh", "bash", "zsh", "dash", "ksh"}
                        and _shell_c_wrapper(words, captures)
                    )
                )
                scripts.update(_wrapper_scripts([protected]))
                for heredoc in scan.heredocs:
                    if not any(token is scan.tokens[heredoc.opener] for token in stage):
                        continue
                    if not _data_consumer(words):
                        scripts.add(heredoc.text)
                    elif not heredoc.quoted:
                        _, children = _capture_words(heredoc.text)
                        for child in children.values():
                            _preflight_capture(child, features, depth)
            features.descriptor |= any(_descriptor_path(token.value, captures) for token in stage)
            scripts.update(
                token.value[1:-1]
                for token in stage
                if not token.quoted
                and token.value.startswith("(")
                and token.value.endswith(")")
                and not token.value.startswith("((")
            )
    for script in scripts:
        _command_preflight(script, features, depth + 1)
