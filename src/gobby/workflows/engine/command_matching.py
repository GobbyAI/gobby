"""Shell-command matching for block-effect ``command_pattern`` selectors.

A Bash command is matched one *executable segment* at a time: the raw text of
each pipeline between unquoted ``&&``, ``||``, ``;``, ``&`` and newline
separators, quotes, pipes and substitutions intact, so the segment-anchored
bundled patterns keep their meaning and a ``curl … | sh`` shape still reads as
one command. Heredoc bodies are stdin data and stay out of the subject unless
something can run them: a body contributes its own subjects, with separate
quote context and line-start anchors, when its consumer is not a known
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

import functools
import re
import shlex
from dataclasses import dataclass
from typing import Literal

from gobby.hooks._ansi_c import SHELL_DIALECTS
from gobby.hooks._normalization_shell import (
    _SHELL_SEQUENCING_TOKENS,
    HeredocBody,
    ShellToken,
    _skip_heredocs,
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
    _piped_to_shell,
    _prepare,
    _python_name,
    _python_program,
    _shell_stdin,
    _unwrap,
    option_word_count,
)
from gobby.hooks.shell_execution import SHELL_WRAPPER_DEPTH, shell_execution

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
_WRAPPER_DEPTH = SHELL_WRAPPER_DEPTH
# Hook events from concurrent sessions interleave, so keep a few dozen commands.
_SELECTOR_SUBJECTS_CACHE_SIZE = 64
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
        "-E",
        "-O",
        "-P",
    }
)
# uv 0.12 global options, accepted before the subcommand and after it.
_UV_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "--cache-dir",
        "--color",
        "--allow-insecure-host",
        "--directory",
        "--project",
        "--config-file",
        "--python-preference",
        "--preview-features",
        "--python-fetch",
    }
)
# Every value-taking option `uv run --help` lists.
_UV_RUN_VALUE_OPTIONS = _UV_GLOBAL_VALUE_OPTIONS | frozenset(
    {
        "--extra",
        "--no-extra",
        "--group",
        "--no-group",
        "--only-group",
        "--no-editable-package",
        "--env-file",
        "-w",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--package",
        "--python-platform",
        "--index",
        "--default-index",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--index-strategy",
        "--keyring-provider",
        "-P",
        "--upgrade-package",
        "--upgrade-group",
        "--resolution",
        "--prerelease",
        "--prerelease-package",
        "--fork-strategy",
        "--exclude-newer",
        "--exclude-newer-package",
        "--no-sources-package",
        "--reinstall-package",
        "--link-mode",
        "-C",
        "--config-setting",
        "--config-settings-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-binary-package",
        "--refresh-package",
        "-p",
        "--python",
    }
)
# Every value-taking option `uvx --help` (alias `uv tool run`) lists.
_UV_TOOL_RUN_VALUE_OPTIONS = _UV_GLOBAL_VALUE_OPTIONS | frozenset(
    {
        "--from",
        "-w",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-c",
        "--constraints",
        "-b",
        "--build-constraints",
        "--overrides",
        "--env-file",
        "--python-platform",
        "--torch-backend",
        "--index",
        "--default-index",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--index-strategy",
        "--keyring-provider",
        "-P",
        "--upgrade-package",
        "--upgrade-group",
        "--resolution",
        "--prerelease",
        "--prerelease-package",
        "--fork-strategy",
        "--exclude-newer",
        "--exclude-newer-package",
        "--no-sources-package",
        "--reinstall-package",
        "--link-mode",
        "-C",
        "--config-setting",
        "--config-settings-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-binary-package",
        "--refresh-package",
        "-p",
        "--python",
    }
)
# A tool command may carry its package's version or extras (`ruff@0.6`, `ruff==0.6`).
_TOOL_SPEC_SUFFIX = re.compile(r"[@\[<>=!~].*", re.DOTALL)


def _uv_run_argv(name: str, args: list[str]) -> list[str] | None:
    """The argv ``uv run``, ``uv tool run`` or ``uvx`` executes; None for other commands."""
    if name == "uvx":
        return _tool_argv(_after_options(args, _UV_TOOL_RUN_VALUE_OPTIONS))
    if name != "uv":
        return None
    subcommand = _after_options(args, _UV_GLOBAL_VALUE_OPTIONS)
    if subcommand[:1] == ["run"]:
        return _after_options(subcommand[1:], _UV_RUN_VALUE_OPTIONS)
    if subcommand[:1] == ["tool"]:
        tool = _after_options(subcommand[1:], _UV_GLOBAL_VALUE_OPTIONS)
        if tool[:1] in (["run"], ["uvx"]):
            return _tool_argv(_after_options(tool[1:], _UV_TOOL_RUN_VALUE_OPTIONS))
    return None


def _tool_argv(words: list[str]) -> list[str]:
    """Drop a package spec suffix from the tool command uv runs."""
    if not words:
        return words
    return [_TOOL_SPEC_SUFFIX.sub("", words[0]), *words[1:]]


def _after_options(
    words: list[str], value_options: frozenset[str], *, infer_long_options: bool = False
) -> list[str]:
    """Drop leading options (and a value option's operand) through ``--``."""
    index = 0
    while index < len(words) and words[index].startswith("-"):
        if words[index] == "--":
            return words[index + 1 :]
        index += option_word_count(
            words[index], value_options, infer_long_options=infer_long_options
        )
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
        if name == "find":
            next_expression = 0
            for index, word in enumerate(unwrapped):
                if index >= next_expression and word in {"-exec", "-execdir", "-ok", "-okdir"}:
                    find_argv = unwrapped[index + 1 :]
                    end = next(
                        (i for i, value in enumerate(find_argv) if value in {";", "+"}),
                        len(find_argv),
                    )
                    if find_argv[:end]:
                        scripts.append(shlex.join(find_argv[:end]))
                    next_expression = index + end + 2
            continue
        if _python_name(name):
            module_args = _python_program(unwrapped)[2]
            if module_args[:1] == ["gobby"]:
                scripts.append(shlex.join(module_args))
        if name == "su":
            for index, word in enumerate(unwrapped[1:], 1):
                if word in {"-c", "--command"} and index + 1 < len(unwrapped):
                    scripts.append(unwrapped[index + 1])
                    break
                if word.startswith("--command="):
                    scripts.append(word.partition("=")[2])
                    break
                command_option = re.fullmatch(r"-[flmpP]*c(.*)", word)
                if command_option:
                    script = command_option[1] or (
                        unwrapped[index + 1] if index + 1 < len(unwrapped) else ""
                    )
                    if script:
                        scripts.append(script)
                    break
        if name in _SHELLS:
            execution = shell_execution(unwrapped)
            if execution and execution.command is not None:
                scripts.append(execution.command)
            continue
        # watch and ssh join every remaining word into the command they run.
        if name == "watch":
            rest = _after_options(unwrapped[1:], _WATCH_VALUE_OPTIONS, infer_long_options=True)
            if rest:
                scripts.append(" ".join(rest))
            continue
        if name == "ssh":
            rest = _after_options(unwrapped[1:], _SSH_VALUE_OPTIONS)
            if len(rest) > 1:
                scripts.append(" ".join(rest[1:]))
            continue
        # These exec their argv directly, so each word stays one word.
        argv = _uv_run_argv(name, unwrapped[1:])
        if argv is not None:
            if argv and resolve_uv_run:
                scripts.append(shlex.join(argv))
            continue
        if unwrapped != words:
            scripts.append(shlex.join(unwrapped))
    return scripts


def _shell_input_scripts(command: str) -> list[str]:
    """Project literal stdin only when a shell will execute it."""
    scripts: list[str] = []
    for dialect in SHELL_DIALECTS:
        try:
            scan = scan_shell_command(command, dialect=dialect)
        except ValueError:
            continue
        start = 0
        for end in range(len(scan.tokens) + 1):
            if end < len(scan.tokens) and (
                scan.tokens[end].quoted
                or scan.tokens[end].value not in {"|", *_SHELL_SEQUENCING_TOKENS}
            ):
                continue
            tokens = scan.tokens[start:end]
            words = _unwrap(_stage_words(tokens))
            start = end + 1
            if not words:
                continue
            if _piped_to_shell(scan.tokens, end) and shell_command_name(words[0]) in {
                "echo",
                "printf",
            }:
                args = words[1:]
                if shell_command_name(words[0]) == "echo":
                    while args and re.fullmatch(r"-[neE]+", args[0]):
                        args = args[1:]
                    scripts.append(" ".join(args))
                else:
                    scripts.extend(args)
            if _shell_stdin(words):
                scripts.extend(
                    tokens[index + 1].value
                    for index, token in enumerate(tokens[:-1])
                    if not token.quoted and token.value == "<<<"
                )
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
    derived = _selector_subjects(command, mask_quoted, resolve_uv_run)
    if derived is None:
        # Code nested past the wrapper bound is unread, so the selector fails closed.
        return True
    pattern_subjects, exemption_text = derived
    if not any(re.search(pattern, subject) for subject in pattern_subjects):
        return False
    return not (not_pattern and re.search(not_pattern, exemption_text))


@functools.lru_cache(maxsize=_SELECTOR_SUBJECTS_CACHE_SIZE)
def _selector_subjects(
    command: str, mask_quoted: bool, resolve_uv_run: bool
) -> tuple[tuple[str, ...], str] | None:
    """Return the pattern subjects and exemption text of ``command``.

    The derivation depends only on these arguments, and every rule's selector
    reads the same command, so one hook event derives it once (#23359).
    Returns None when a wrapper nests past the bound.
    """
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
        # A literal execution wrapper (``bash -c``, ``eval``, ``xargs``,
        # ``timeout``, ``watch``, ``ssh``) runs a further command string that the
        # scanner reads as one segment's words. Resolve it through these same
        # rules, to a bounded depth, so a nested wrapped invocation such as
        # ``bash -c "bash -c '…'"`` still matches (#23134).
        scripts = _wrapper_scripts(stages, resolve_uv_run=resolve_uv_run)
        scripts.extend(_shell_input_scripts(text))
        if scripts and depth >= _WRAPPER_DEPTH:
            return None
        for script in scripts:
            pending.extend((inner, depth + 1) for inner in executable_command_subjects(script))
    return tuple(pattern_subjects), exemption_text


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


def _subjects(
    command: str,
    depth: int,
    *,
    heredoc_source: Literal["shell", "language"] | None = None,
    correlated_subjects: list[str] | None = None,
) -> list[str]:
    try:
        scan = scan_shell_command(command)
    except ValueError:
        return [command]
    # Language source has its own syntax; a variable named cat is not a data sink.
    if heredoc_source == "language" or (heredoc_source == "shell" and not scan.heredocs):
        return [command]
    correlations = [] if correlated_subjects is None else correlated_subjects
    split_substitution = False
    for start, end in scan.spans:
        opening = command.find("$(", start, end)
        if opening >= 0:
            try:
                split_substitution |= (
                    _substitution_end(command, opening + 2, False, depth + 1) >= end
                )
            except ValueError:
                continue
    if split_substitution and not _runs_substitution_output(scan.tokens):
        resolved = _resolve_substitutions(command, depth, correlations)
        if resolved[0] != command:
            projected = _subjects(resolved[0], depth, correlated_subjects=correlations)
            projected.extend(resolved[1:])
            if correlated_subjects is None:
                projected.extend(correlations)
            return projected
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
    subjects: list[str] = []
    for text, segment in zip(raw, segments, strict=True):
        if _runs_substitution_output(scan.tokens[segment.first : segment.last + 1]):
            subjects.append(text)
        else:
            subjects.extend(_resolve_substitutions(text, depth, correlations))
    for heredoc in scan.heredocs:
        owner = next(
            index
            for index, segment in enumerate(segments)
            if segment.first <= heredoc.opener <= segment.last
        )
        segment = segments[owner]
        tokens = scan.tokens[segment.first : segment.last + 1]
        if _heredoc_may_execute(tokens, raw[owner], heredoc, heredoc.opener - segment.first):
            # Each executed body has its own quote context. Joining it to the
            # owner or a sibling lets an interpreter quote hide later commands.
            subjects.extend(
                _subjects(
                    heredoc.text,
                    depth,
                    heredoc_source=(
                        "shell"
                        if any(
                            (words := _unwrap(_stage_words(stage)))
                            and (_shell_stdin(words) or shell_command_name(words[0]) == "ssh")
                            for stage in _pipeline_stages(tokens)
                        )
                        else "language"
                    ),
                    correlated_subjects=correlations,
                )
            )
        elif not heredoc.quoted:
            for span in _substitution_spans(heredoc.text):
                subjects.extend(_subjects(span, depth + 1, correlated_subjects=correlations))
    if correlated_subjects is None:
        subjects.extend(correlations)
    return subjects


def _resolve_substitutions(subject: str, depth: int, correlations: list[str]) -> list[str]:
    """Extract each command substitution into independent executable subjects.

    The scanner reads ``"$(cat <<'EOF' … EOF)"`` as one quoted token, so a
    heredoc opened inside a substitution never reaches the data-sink check.
    Running the body through the same segment rules drops what it only prints
    and keeps what it executes. Leave empty substitution delimiters in the owner
    to preserve word boundaries without importing the body's quote context.
    Also retain an owner with resolved bodies for patterns that correlate a
    command with its argument text. Independent subjects remain authoritative
    for execution matching even when that additional subject has odd quotes.
    Collect correlations separately so enclosing substitutions never splice
    those additional projections back into their own bodies.
    A body that cannot be delimited stays whole, and single quotes make a
    substitution literal text.
    """
    out: list[str] = []
    correlated: list[str] = []
    executed: list[str] = []
    quote = ""
    index = 0
    line_start = 0
    while index < len(subject):
        char = subject[index]
        if char == "\\" and quote != "'":
            out.append(subject[index : index + 2])
            correlated.append(subject[index : index + 2])
            index += 2
            continue
        if quote not in {"'", "$"} and (subject.startswith("$(", index) or char == "`"):
            tick = char == "`"
            start = index + (1 if tick else 2)
            try:
                end = _substitution_end(subject, start, tick, depth + 1)
            except ValueError:
                return [subject]
            body = subject[start:end]
            bodies = _subjects(body, depth + 1, correlated_subjects=correlations) if body else []
            executed.extend(bodies)
            out.append(f"{subject[index:start]}{subject[end]}")
            correlated.append(f"{subject[index:start]}{' '.join(bodies)}{subject[end]}")
            index = end + 1
            continue
        if not quote and subject.startswith("$'", index):
            quote = "$"
            out.append("$'")
            correlated.append("$'")
            index += 2
            continue
        if char in "\"'":
            if not quote:
                quote = char
            elif quote == char or (quote == "$" and char == "'"):
                quote = ""
        elif not quote and char == "#" and (index == 0 or subject[index - 1] in " \t\n;|&()"):
            end = subject.find("\n", index)
            end = len(subject) if end < 0 else end
            out.append(subject[index:end])
            correlated.append(subject[index:end])
            index = end
            continue
        if char == "\n" and not quote:
            try:
                line = scan_shell_command(subject[line_start:index])
            except ValueError:
                # Comments and ANSI-C quotes can diverge from this lightweight
                # tracker. Keep the complete subject for conservative matching.
                return [subject]
            if not line.tokens or line.tokens[-1].value not in _CONTINUATION_OPERATORS:
                end = _skip_heredocs(subject, line.tokens, index + 1)
                out.append(subject[index:end])
                correlated.append(subject[index:end])
                index = end
                line_start = end
                continue
        out.append(char)
        correlated.append(char)
        index += 1
    if executed:
        correlations.append("".join(correlated))
    return ["".join(out), *executed]


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
