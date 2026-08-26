"""Shell-command matching for block-effect ``command_pattern`` selectors.

A Bash command is matched one *executable segment* at a time: the raw text of
each pipeline between unquoted ``&&``, ``||``, ``;``, ``&`` and newline
separators, quotes, pipes and substitutions intact, so the segment-anchored
bundled patterns keep their meaning and a ``curl … | sh`` shape still reads as
one command. Heredoc bodies are stdin data and stay out of the subject unless
something can run them: a body is re-attached to its opener segment (after a
newline, so line-start anchors still see it) when any pipeline stage is not a
known data sink, when the segment process-substitutes output, when an unquoted
delimiter leaves ``$(`` or backtick expansion live, or when the heredoc never
terminates.

``command_pattern`` must match one subject. ``command_not_pattern`` exempts the
command when it matches the executable text as a whole, because an exemption
such as an exported test environment can be established by an earlier
segment (#21056).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gobby.hooks._normalization_shell import (
    _SHELL_SEQUENCING_TOKENS,
    HeredocBody,
    ShellToken,
    _strip_shell_wrappers,
    is_fd_duplication_token,
    is_shell_input_redirection_token,
    is_shell_output_redirection_token,
    scan_shell_command,
)
from gobby.hooks.code_navigation import shell_command_name

# Commands whose standard input is never interpreted as code. Every other
# consumer — shells, language interpreters, ``ssh``, ``xargs``, ``eval``,
# unknown tools — fails closed and keeps its heredoc body in the subject.
HEREDOC_DATA_CONSUMERS = frozenset({"cat", "tee", "git", "gh"})
_LIVE_EXPANSION_RE = re.compile(r"\$\(|`")
_OUTPUT_PROCESS_SUBSTITUTION_RE = re.compile(r">\(")
# A newline right after one of these continues the same command list.
_CONTINUATION_OPERATORS = frozenset({"|", "&&", "||"})


@dataclass(frozen=True, slots=True)
class _Segment:
    first: int
    last: int


def mask_quoted_spans(command: str) -> str:
    """Blank shell string data so command patterns see only code.

    Single-quoted spans are always data. A double-quoted span stays visible
    when it contains ``$(`` or a backtick, because command substitution inside
    it still executes — the coarse check fails toward a false positive, never
    toward letting an invocation hide. Quote characters themselves are kept so
    the code structure around the span survives; masked characters become
    spaces, which also removes newline segment boundaries inside string data
    (the way a multi-line commit message tripped command-position anchors,
    #20887).
    """
    out = list(command)
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            end = command.find("'", i + 1)
            end = n if end == -1 else end
            for j in range(i + 1, end):
                out[j] = " "
            i = end + 1
        elif ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            span = command[i + 1 : j]
            if "$(" not in span and "`" not in span:
                for k in range(i + 1, j):
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def command_patterns_match(
    command: str,
    *,
    pattern: str | None,
    not_pattern: str | None = None,
    mask_quoted: bool = False,
) -> bool:
    """Return whether ``command`` selects a block effect carrying these patterns.

    Without a ``pattern`` the selector is unconstrained and matches.
    """
    if not pattern:
        return True
    subjects = executable_command_subjects(command)
    if mask_quoted:
        subjects = [mask_quoted_spans(subject) for subject in subjects]
    if not any(re.search(pattern, subject) for subject in subjects):
        return False
    return not (not_pattern and re.search(not_pattern, "\n".join(subjects)))


def executable_command_subjects(command: str) -> list[str]:
    """Return the match subjects of ``command``: one per executable segment.

    A command the scanner cannot parse (unclosed quote) or that has no tokens
    is matched whole, the fail-closed reading.
    """
    try:
        scan = scan_shell_command(command)
    except ValueError:
        return [command]
    segments = _split_segments(scan.tokens)
    if not segments:
        return [command]
    raw = [
        command[scan.spans[segment.first][0] : scan.spans[segment.last][1]] for segment in segments
    ]
    subjects = list(raw)
    for heredoc in scan.heredocs:
        owner = next(
            index
            for index, segment in enumerate(segments)
            if segment.first <= heredoc.opener <= segment.last
        )
        segment = segments[owner]
        if _heredoc_may_execute(scan.tokens[segment.first : segment.last + 1], raw[owner], heredoc):
            subjects[owner] = f"{subjects[owner]}\n{heredoc.text}"
    return subjects


def _split_segments(tokens: list[ShellToken]) -> list[_Segment]:
    segments: list[_Segment] = []
    first: int | None = None
    for index, token in enumerate(tokens):
        if token.quoted or token.value not in _SHELL_SEQUENCING_TOKENS:
            if first is None:
                first = index
            continue
        if token.value == "\n" and index and _is_continuation_operator(tokens[index - 1]):
            continue
        if first is not None:
            segments.append(_Segment(first, index - 1))
            first = None
    if first is not None:
        segments.append(_Segment(first, len(tokens) - 1))
    return segments


def _is_continuation_operator(token: ShellToken) -> bool:
    return not token.quoted and token.value in _CONTINUATION_OPERATORS


def _heredoc_may_execute(tokens: list[ShellToken], raw: str, heredoc: HeredocBody) -> bool:
    if not heredoc.terminated:
        return True
    if not heredoc.quoted and _LIVE_EXPANSION_RE.search(heredoc.text):
        return True
    if _OUTPUT_PROCESS_SUBSTITUTION_RE.search(raw):
        return True
    for stage in _pipeline_stages(tokens):
        consumer = _stage_command(stage)
        if consumer is not None and shell_command_name(consumer) not in HEREDOC_DATA_CONSUMERS:
            return True
    return False


def _pipeline_stages(tokens: list[ShellToken]) -> list[list[ShellToken]]:
    stages: list[list[ShellToken]] = [[]]
    for token in tokens:
        if not token.quoted and token.value == "|":
            stages.append([])
        elif not (not token.quoted and token.value == "\n"):
            stages[-1].append(token)
    return stages


def _stage_command(tokens: list[ShellToken]) -> str | None:
    """Return a pipeline stage's command word, or None for a bare redirection."""
    words: list[str] = []
    skip_operand = False
    for token in tokens:
        if skip_operand:
            skip_operand = False
            continue
        if is_fd_duplication_token(token):
            continue
        if is_shell_input_redirection_token(token) or is_shell_output_redirection_token(token):
            skip_operand = True
            continue
        words.append(token.value)
    words = _strip_shell_wrappers(words)
    return words[0] if words else None
