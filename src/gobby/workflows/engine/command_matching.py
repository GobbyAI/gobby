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
import shlex
from dataclasses import dataclass

from gobby.hooks._ansi_c import SHELL_DIALECTS
from gobby.hooks._normalization_shell import (
    _SHELL_SEQUENCING_TOKENS,
    HeredocBody,
    ShellToken,
    _strip_shell_wrappers,
    _substitution_end,
    is_fd_duplication_token,
    is_shell_input_redirection_token,
    is_shell_output_redirection_token,
    scan_shell_command,
)
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import (
    _SHELLS,
    _prepare,
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

    An unquoted ``#`` starts a comment through the newline; a quote inside it is
    prose, not the start of a string (#23134). Without this, an apostrophe in a
    comment reads as an unterminated single-quoted span and blanks the real
    invocation on the following line.
    """
    return _blank_quoted_chars(command, chars=None)


# Wrapper scripts nest (``bash -c "bash -c '…'"``); resolve them to a bounded
# depth, matching the substitution-recursion guard used by the shell scanner.
_WRAPPER_DEPTH = 8
# Value-taking options for wrapper tools whose executed command follows them.
# procps watch: only these take a separate operand; -d/-p/-x and the rest are flags.
_WATCH_VALUE_OPTIONS = frozenset({"-n", "--interval", "-q", "--equexit"})
_SSH_VALUE_OPTIONS = frozenset(
    {
        "-p",
        "-l",
        "-i",
        "-o",
        "-F",
        "-c",
        "-m",
        "-b",
        "-e",
        "-D",
        "-L",
        "-R",
        "-W",
        "-J",
        "-S",
        "-w",
        "-B",
        "-I",
        "-Q",
    }
)


_UV_RUN_VALUE_OPTIONS = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--project",
        "--directory",
        "--python",
        "-p",
        "--package",
        "--extra",
        "--group",
        "--only-group",
        "--no-group",
        "--env-file",
        "--index",
        "--find-links",
        "-f",
        "--config-file",
        "--cache-dir",
    }
)


def _after_options(words: list[str], value_options: frozenset[str]) -> list[str]:
    """Drop leading options (and a known value option's operand)."""
    index = 0
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in value_options else 1
    return words[index:]


def _decoded_stages(subject: str) -> list[list[str]]:
    """Return each pipeline stage's words as bash and as zsh would run them.

    The shells decode some ``$'...'`` escapes differently, so a word can name a
    command under one and not the other; both readings are kept.
    """
    stages: list[list[str]] = []
    for dialect in SHELL_DIALECTS:
        try:
            scan = scan_shell_command(subject, dialect=dialect)
        except ValueError:
            continue
        for stage in _pipeline_stages(scan.tokens):
            words = _stage_words(stage)
            # A subshell runs its body: `(time cmd)` scans as `(time`, `cmd)`.
            if words and words[0].startswith("("):
                head = words[0].lstrip("(")
                words = [head, *words[1:]] if head else words[1:]
                tail = words[-1].rstrip(")") if words else ""
                words = [*words[:-1], tail] if tail else words[:-1]
            if words and words not in stages:
                stages.append(words)
    return stages


def _wrapper_scripts(stages: list[list[str]], *, resolve_uv_run: bool = True) -> list[str]:
    """Return the command strings a segment's literal wrappers would execute.

    Only arguments that are code count: a shell ``-c`` string, ``eval``'s
    arguments, ``xargs``/``timeout``/``watch``/``ssh`` targets and any other
    prefix ``_unwrap`` strips. A data argument to an ordinary command
    (``git commit -m``) is not a wrapper and yields nothing.
    """
    scripts: list[str] = []
    for words in stages:
        unwrapped = _unwrap(words)
        if not unwrapped:
            continue
        name = shell_command_name(unwrapped[0])
        if name in _SHELLS:
            for index, arg in enumerate(unwrapped[1:], 1):
                if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                    # `--` ends the options; the script is the word after it.
                    script = unwrapped[index + 1 : index + 3]
                    if script[:1] == ["--"]:
                        script = script[1:]
                    scripts.extend(script[:1])
                    break
            continue
        # watch and ssh join every remaining word into the command they run.
        if name == "watch":
            rest = _after_options(unwrapped[1:], _WATCH_VALUE_OPTIONS)
            if rest:
                scripts.append(" ".join(rest))
            continue
        if name == "ssh":
            rest = _after_options(unwrapped[1:], _SSH_VALUE_OPTIONS)
            if len(rest) > 1:
                scripts.append(" ".join(rest[1:]))
            continue
        # These exec their argv directly, so each word stays one word.
        if name == "uv" and unwrapped[1:2] == ["run"]:
            rest = _after_options(unwrapped[2:], _UV_RUN_VALUE_OPTIONS)
            if rest and resolve_uv_run:
                scripts.append(shlex.join(rest))
            continue
        if unwrapped != words:
            scripts.append(shlex.join(unwrapped))
    return scripts


def command_patterns_match(
    command: str,
    *,
    pattern: str | None,
    not_pattern: str | None = None,
    mask_quoted: bool = False,
    resolve_uv_run: bool = True,
) -> bool:
    """Return whether ``command`` selects a block effect carrying these patterns.

    Without a ``pattern`` the selector is unconstrained and matches.
    """
    if not pattern:
        return True
    subjects = executable_command_subjects(command)
    exemption_text = "\n".join(subjects)
    mask_one = mask_quoted_spans if mask_quoted else _mask_quoted_command_boundaries
    pattern_subjects: list[str] = []
    pending: list[tuple[str, int]] = [(subject, 0) for subject in subjects]
    while pending:
        text, depth = pending.pop()
        pattern_subjects.append(mask_one(text))
        # Quotes and backslashes inside a word vanish before it runs, so a
        # quoted or escaped command name still runs that command. Match each
        # stage's decoded words too, re-quoted so a word holding spaces stays data.
        stages = _decoded_stages(text)
        pattern_subjects.extend(mask_one(shlex.join(words)) for words in stages)
        if depth >= _WRAPPER_DEPTH:
            continue
        # A literal execution wrapper (``bash -c``, ``eval``, ``xargs``,
        # ``timeout``, ``watch``, ``ssh``) runs a further command string that the
        # scanner reads as one segment's words. Resolve it through these same
        # rules, to a bounded depth, so a nested wrapped invocation such as
        # ``bash -c "bash -c '…'"`` still matches (#23134).
        for script in _wrapper_scripts(stages, resolve_uv_run=resolve_uv_run):
            pending.extend((inner, depth + 1) for inner in executable_command_subjects(script))
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
        elif ch == "#" and (i == 0 or command[i - 1] in " \t\n;|&()"):
            # An unquoted ``#`` at word start comments through the newline; the
            # text inside it is prose, so a quote there must not open a span.
            end = command.find("\n", i)
            i = n if end < 0 else end
        elif command.startswith("$'", i):
            # ANSI-C `$'...'`: a backslash escapes the next character.
            end = i + 2
            while end < n and command[end] != "'":
                end += 2 if command[end] == "\\" else 1
            end = min(end, n)
            for j in range(i + 2, end):
                if chars is None or command[j] in chars:
                    out[j] = " "
            i = end + 1
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
    # A quoted substitution's redirects follow its word with spans inside it,
    # so a segment's text runs from its earliest start to its latest end.
    raw: list[str] = []
    for segment in segments:
        segment_spans = scan.spans[segment.first : segment.last + 1]
        raw.append(
            command[min(start for start, _ in segment_spans) : max(end for _, end in segment_spans)]
        )
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
