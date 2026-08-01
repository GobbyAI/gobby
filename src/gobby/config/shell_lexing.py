"""Shell command tokenization shared by validation detection and rule conditions.

Splits a command string into the segments a shell would run, so callers can ask
what a command actually invokes rather than pattern-matching raw text.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

SHELL_SEGMENT_SEPARATORS = {"&&", "||", ";", "|", "|&", "&", "\n"}
_SHELL_PUNCTUATION = ";&|<>\n"
_SHELL_REDIRECTION_RE = re.compile(r"^(?:[<>]+|[<>]&|&[<>])$")
# `(?<!<)` keeps the `<<<` herestring, which has no body, out of the match.
_HEREDOC_RE = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


@dataclass(frozen=True)
class ParsedShellCommand:
    """Tokenized shell segments and operators joining them."""

    segments: tuple[tuple[str, ...], ...]
    operators: tuple[str, ...]


def shell_command_segments(command: str) -> list[list[str]]:
    """Split a shell command into token segments separated by shell operators."""
    return [list(segment) for segment in parse_shell_command(command).segments]


def parse_shell_command(command: str) -> ParsedShellCommand:
    """Tokenize a shell command while preserving control operators."""
    try:
        lexer = shlex.shlex(
            _strip_heredoc_bodies(command),
            posix=True,
            punctuation_chars=_SHELL_PUNCTUATION,
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return ParsedShellCommand((), ())

    segments: list[tuple[str, ...]] = []
    operators: list[str] = []
    current: list[str] = []
    skip_redirection_target = False
    for token in tokens:
        if skip_redirection_target:
            skip_redirection_target = False
            continue
        if token in SHELL_SEGMENT_SEPARATORS:
            if current:
                segments.append(tuple(current))
            current = []
            operators.append(token)
            continue
        if _SHELL_REDIRECTION_RE.search(token):
            if current and current[-1].isdigit():
                current.pop()
            skip_redirection_target = True
            continue
        current.append(token)
    if current:
        segments.append(tuple(current))
    return ParsedShellCommand(tuple(segments), tuple(operators))


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies so their contents never parse as commands.

    A newline separates segments, so an unstripped body turns each of its lines
    into a pseudo-command — enough for a merely documented test invocation to be
    credited as a real validation run.
    """
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for dedent, _quote, delimiter in _HEREDOC_RE.findall(line):
            while index < len(lines):
                terminator = lines[index].rstrip()
                index += 1
                if (terminator.lstrip("\t") if dedent else terminator) == delimiter:
                    break
    return "\n".join(kept)


def safe_split(value: str) -> list[str]:
    """Split `value` into tokens, falling back to whitespace on unbalanced quotes."""
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()
