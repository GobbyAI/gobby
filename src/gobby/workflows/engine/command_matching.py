"""Shell-command matching for block-effect ``command_pattern`` selectors.

A Bash command is matched one *executable segment* at a time: the raw text of
each pipeline between unquoted ``&&``, ``||``, ``;``, ``&`` and newline
separators, quotes, pipes and substitutions intact, so the segment-anchored
bundled patterns keep their meaning and a ``curl … | sh`` shape still reads as
one command. Heredoc bodies are stdin data and stay out of the subject unless
something can run them: a body is re-attached to its opener segment (after a
newline, so line-start anchors still see it) when its consumer is not a known
data sink, when the segment process-substitutes output, when a downstream
pipeline stage is a shell or ``eval``, or when the heredoc never terminates. A
data sink's body stays data even when its output pipes onward, and an unquoted
body contributes only its command-substitution spans — the shell runs those,
never the literal body.

A command substitution is a command list of its own, and the scanner reads it
as a single token, so its body is resolved through these same rules before it
re-enters the segment: ``git commit -m "$(cat <<'EOF' … EOF)"`` keeps only
``cat <<'EOF'`` while ``"$(uv run pytest)"`` keeps its invocation. A segment
that runs what a substitution prints (``sh -c``, ``eval``) keeps the body
whole, the fail-closed reading.

``command_pattern`` must match one subject. Quoted ``;``, ``&``, ``|``, ``(``,
and backticks in that subject are not command boundaries — they are blanked
before the pattern runs — so a lookbehind such as ``(?<=[;&|(`\\n])`` cannot
treat ``gcode grep -E 'pytest|vitest'`` as a ``vitest`` invocation.
``command_not_pattern`` exempts over the unblanked executable text — masked or
not — because an exemption such as an exported test environment can be
established by an earlier segment (#21056) and a quoted path such as
``pytest 'tests/x.py'`` must still exempt under ``mask_quoted``.
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
from gobby.hooks.provider_launch_guard import (
    _SHELLS,
    _prepare,
    _substitution_end,
    _unwrap,
)

# Commands whose standard input is never interpreted as code. Every other
# consumer — shells, language interpreters, ``ssh``, ``xargs``, ``eval``,
# unknown tools — fails closed and keeps its heredoc body in the subject.
HEREDOC_DATA_CONSUMERS = frozenset({"cat", "tee", "git", "gh"})
_OUTPUT_PROCESS_SUBSTITUTION_RE = re.compile(r">\(")
# A newline right after one of these continues the same command list.
_CONTINUATION_OPERATORS = frozenset({"|", "&&", "||"})
# Lookbehind class used by bundled command-position patterns, minus newline
# (quoted newlines stay boundaries unless ``mask_quoted`` blanks the span).
_QUOTED_COMMAND_BOUNDARIES = frozenset(";|&(`")


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
    return _blank_quoted_chars(command, chars=None)


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
    exemption_text = "\n".join(subjects)
    if mask_quoted:
        pattern_subjects = [mask_quoted_spans(subject) for subject in subjects]
    else:
        pattern_subjects = [_mask_quoted_command_boundaries(subject) for subject in subjects]
    if not any(re.search(pattern, subject) for subject in pattern_subjects):
        return False
    return not (not_pattern and re.search(not_pattern, exemption_text))


def _mask_quoted_command_boundaries(command: str) -> str:
    """Blank ``; & | (`` and backticks inside quotes so lookbehinds miss them."""
    return _blank_quoted_chars(command, chars=_QUOTED_COMMAND_BOUNDARIES)


def _blank_quoted_chars(command: str, *, chars: frozenset[str] | None) -> str:
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
                if chars is None or command[j] in chars:
                    out[j] = " "
            i = end + 1
        elif ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            span = command[i + 1 : j]
            if "$(" not in span and "`" not in span:
                for k in range(i + 1, j):
                    if chars is None or command[k] in chars:
                        out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def executable_command_subjects(command: str) -> list[str]:
    """Return the match subjects of ``command``: one per executable segment.

    A command the scanner cannot parse (unclosed quote) or that has no tokens
    is matched whole, the fail-closed reading.
    """
    return _subjects(command, 0)


def _subjects(command: str, depth: int) -> list[str]:
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
    subjects = [
        text
        if _runs_substitution_output(scan.tokens[segment.first : segment.last + 1])
        else _resolve_substitutions(text, depth)
        for text, segment in zip(raw, segments, strict=True)
    ]
    for heredoc in scan.heredocs:
        owner = next(
            index
            for index, segment in enumerate(segments)
            if segment.first <= heredoc.opener <= segment.last
        )
        segment = segments[owner]
        tokens = scan.tokens[segment.first : segment.last + 1]
        if _heredoc_may_execute(tokens, raw[owner], heredoc, heredoc.opener - segment.first):
            subjects[owner] = f"{subjects[owner]}\n{heredoc.text}"
        elif not heredoc.quoted:
            subjects[owner] += "".join(f"\n{span}" for span in _substitution_spans(heredoc.text))
    return subjects


def _resolve_substitutions(subject: str, depth: int) -> str:
    """Replace each command substitution with the commands it actually runs.

    The scanner reads ``"$(cat <<'EOF' … EOF)"`` as one quoted token, so a
    heredoc opened inside a substitution never reaches the data-sink check.
    Running the body through the same segment rules drops what it only prints
    and keeps what it executes. A body that cannot be delimited stays whole,
    and single quotes make a substitution literal text.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(subject):
        char = subject[index]
        if char == "\\" and quote != "'":
            out.append(subject[index : index + 2])
            index += 2
            continue
        if quote != "'" and (subject.startswith("$(", index) or char == "`"):
            tick = char == "`"
            start = index + (1 if tick else 2)
            try:
                end = _substitution_end(subject, start, tick, depth + 1)
            except ValueError:
                return subject
            body = "\n".join(_subjects(subject[start:end], depth + 1))
            out.append(f"{subject[index:start]}{body}{subject[end]}")
            index = end + 1
            continue
        if char in "\"'":
            quote = "" if quote == char else quote or char
        out.append(char)
        index += 1
    return "".join(out)


def _runs_substitution_output(tokens: list[ShellToken]) -> bool:
    """Whether a segment runs what its substitutions print (``sh -c "$(…)"``)."""
    for stage in _pipeline_stages(tokens):
        words = _unwrap(_stage_words(stage))
        if words and shell_command_name(words[0]) in _SHELLS:
            return True
    return False


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


def _heredoc_may_execute(
    tokens: list[ShellToken], raw: str, heredoc: HeredocBody, opener: int
) -> bool:
    if not heredoc.terminated:
        return True
    if _OUTPUT_PROCESS_SUBSTITUTION_RE.search(raw):
        return True
    stages = _pipeline_stages(tokens)
    owner = sum(not token.quoted and token.value == "|" for token in tokens[:opener])
    consumer = _strip_shell_wrappers(_stage_words(stages[owner]))
    if consumer and shell_command_name(consumer[0]) not in HEREDOC_DATA_CONSUMERS:
        return True
    # A data consumer's output is still data downstream, unless a shell runs it.
    for stage in stages[owner + 1 :]:
        words = _unwrap(_stage_words(stage))
        if words and shell_command_name(words[0]) in _SHELLS:
            return True
    return False


def _substitution_spans(body: str) -> list[str]:
    """Return the command substitutions an unquoted heredoc body runs.

    A body whose substitutions cannot be delimited is matched whole.
    """
    try:
        return _prepare(body, 0, data=True)[1]
    except ValueError:
        return [body]


def _pipeline_stages(tokens: list[ShellToken]) -> list[list[ShellToken]]:
    stages: list[list[ShellToken]] = [[]]
    for token in tokens:
        if not token.quoted and token.value == "|":
            stages.append([])
        elif not (not token.quoted and token.value == "\n"):
            stages[-1].append(token)
    return stages


def _stage_words(tokens: list[ShellToken]) -> list[str]:
    """Return a pipeline stage's words without its redirections."""
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
    return words
