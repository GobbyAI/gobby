"""Shell tool identity and command-token helpers."""

import os
import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from gobby.hooks._ansi_c import ShellDialect, decode_ansi_c_escape, opens_ansi_c_quote
from gobby.hooks._case_syntax import CaseSyntax, _balanced_parens_end, pid_parameter_end
from gobby.hooks._normalization_env import UNRESOLVED_DIRECTORY, strip_shell_wrappers
from gobby.hooks._shell_syntax import (
    _compound_comparison_operators,
    _scan_unquoted_shell_operator,
    joined_prefix_end,
    skip_line_continuations,
)

# Tools that run shell commands. ``Bash`` is the canonical runtime name, but
# several adapters and transcripts use shell aliases that should behave the same.
_SHELL_TOOLS = frozenset(
    {
        "Bash",
        "bash",
        "shell",
        "run_command",
        "run_shell_command",
        "RunShellCommand",
        "ShellTool",
        "commandExecution",
        "exec_command",
        "functions.exec_command",
    }
)

_SHELL_CHAIN_TOKENS = frozenset({"&&", "||", ";", "|", "&", "\n"})
# Chain tokens that sequence a *separate* command, unlike ``|`` which only pipes
# the leading command's output into a filter. A gcode navigation piped to a
# read-only filter (``gcode symbol <id> | jq``) is still navigation; one joined to
# another command via these is not, so those stay classified as ``execute``.
_SHELL_SEQUENCING_TOKENS = frozenset({"&&", "||", ";", "&", "\n"})
_SHELL_INPUT_REDIRECTION_TOKENS = frozenset({"<", "<<", "<<-", "<<<"})
# Heredoc openers queue a delimiter; everything from the next command-terminating
# newline to the delimiter line is stdin data, not shell syntax.
_HEREDOC_OPERATORS = frozenset({"<<", "<<-"})
# ``>&`` (csh-style redirect stdout+stderr to a file) only reaches token form when
# it is *not* followed by digits or ``-``; those forms scan as fd duplication.
_SHELL_OUTPUT_REDIRECTION_TOKENS = frozenset(
    {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", ">&"}
)
_SHELL_CONTROL_TOKENS = (
    _SHELL_CHAIN_TOKENS | _SHELL_INPUT_REDIRECTION_TOKENS | _SHELL_OUTPUT_REDIRECTION_TOKENS
)
_FD_OUTPUT_REDIRECTION_RE = re.compile(r"^\d+>>?$")
# Fd duplication (``2>&1``, ``>&2``, ``0<&3``, ``2>&-``) rebinds descriptors without
# opening files, so it is neither a mutating redirection nor a segment separator.
_FD_DUP_TOKEN_RE = re.compile(r"^\d*[<>]&(?:\d+|-)$")

# Characters that strongly imply an inline sed/awk script rather than a file path.
_SCRIPT_LIKE_CHARS = frozenset({"{", "}", "$", ";", "(", ")"})
# `$` opening a variable (`$VAR`, `${VAR}`), command substitution (`$(cmd)`),
# positional parameter (`$1`), special parameter, or backtick substitution.
_UNEXPANDED_SHELL_REFERENCE = re.compile(r"\$[\w{(@*?#$!-]|`")
# `$(cmd)` (but not arithmetic `$((`) or a backtick substitution: commands that run.
_COMMAND_SUBSTITUTION = re.compile(r"\$(?:\\\n)*\((?!(?:\\\n)*\()|`")
# Stop before '+' so compound-word handling can recognize a mid-word extglob.
_PLAIN_WORD_RUN = re.compile(r"[A-Za-z0-9_./:=,%-]+")
_PLAIN_SPACE_RUN = re.compile(r"[ \t]+")
_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_./:=,+%{}-]+(?:\(\)[A-Za-z0-9_./:=,+%{}-]*)*|;")
_PLAIN_COMMAND = re.compile(r"[A-Za-z0-9_./:=,+%{}(); \t-]*")
_MAX_SUBSTITUTION_DEPTH = 24
_KNOWN_NAVIGATION_SHELL_REFERENCE = re.compile(
    r"(?<![\\$])\$(?:\{(?P<braced>HOME|PWD|TMPDIR)\}|(?P<bare>HOME|PWD|TMPDIR)(?!\w))"
)


@dataclass(frozen=True, slots=True)
class ShellToken:
    """A shell token plus whether quoting or escaping changed its literal value."""

    value: str
    quoted: bool = False


@dataclass(frozen=True, slots=True)
class HeredocBody:
    """One heredoc body and the facts that decide whether it is data or code.

    ``quoted`` records a quoted delimiter (``<<'EOF'``), which disables
    expansion inside the body. ``opener`` is the index of the delimiter token
    in the owning scan. Source offsets cover the body and its closing
    delimiter, which may precede a pipeline's next stage.
    """

    text: str
    quoted: bool
    terminated: bool
    opener: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ShellScan:
    """Tokens of a shell command, their source spans, and its heredoc bodies."""

    tokens: list[ShellToken]
    spans: list[tuple[int, int]]
    heredocs: list[HeredocBody]


@dataclass(frozen=True, slots=True)
class _PendingHeredoc:
    delimiter: str
    strip_tabs: bool
    quoted: bool
    opener: int


def tokenize_shell_command(
    command: str,
    *,
    heredoc_bodies: list[str] | None = None,
) -> list[ShellToken]:
    """Split a shell command into tokens while preserving quoted operator literals.

    When ``heredoc_bodies`` is provided, terminated heredoc body text is
    appended to it in encounter order.
    """
    scan = scan_shell_command(command)
    if heredoc_bodies is not None:
        heredoc_bodies.extend(body.text for body in scan.heredocs if body.terminated)
    return scan.tokens


def scan_shell_command(command: str, *, dialect: ShellDialect = "bash") -> ShellScan:
    """Return an independent view of a bounded, immutable lexical result."""
    if not 128 <= len(command) <= 32768:
        return _scan_shell_command(command, dialect=dialect)
    tokens, spans, heredocs = _cached_shell_scan(command, dialect)
    return ShellScan(list(tokens), list(spans), list(heredocs))


@lru_cache(maxsize=64)
def _cached_shell_scan(
    command: str, dialect: ShellDialect
) -> tuple[tuple[ShellToken, ...], tuple[tuple[int, int], ...], tuple[HeredocBody, ...]]:
    # Nested wrappers inspect the same bodies repeatedly. Cache only bounded
    # input and immutable values; callers remain free to mutate their lists.
    scan = _scan_shell_command(command, dialect=dialect)
    return tuple(scan.tokens), tuple(scan.spans), tuple(scan.heredocs)


def _scan_shell_command(command: str, *, dialect: ShellDialect = "bash") -> ShellScan:
    """Tokenize ``command`` and keep each token's source span and heredoc bodies.

    Spans index into ``command`` with quotes and escapes included, so a token
    range maps back to its raw text. Raises ``ValueError`` on an unclosed
    quote or a trailing escape. ``dialect`` selects how ``$'...'`` escapes decode.
    """
    if _PLAIN_COMMAND.fullmatch(command) is not None:
        words = list(_PLAIN_TOKEN.finditer(command))
        if "".join(word[0] for word in words) == "".join(command.split()) and not any(
            word[0] in {"case", "esac"} for word in words
        ):
            return ShellScan(
                [ShellToken(word[0]) for word in words], [word.span() for word in words], []
            )
    tokens: list[ShellToken] = []
    spans: list[tuple[int, int]] = []
    heredocs: list[HeredocBody] = []
    current: list[str] = []
    quoted = False
    in_single_quote = False
    ansi_c = False
    in_double_quote = False
    escaped = False
    token_start: int | None = None
    pending_heredocs: list[_PendingHeredoc] = []
    heredoc_operator: str | None = None
    comparison_operators: set[int] = set()
    cases = CaseSyntax()
    arm_terminator: int | None = None
    # Output redirects of substitutions inside the current double-quoted word.
    quoted_substitution_redirects: list[tuple[ShellToken, tuple[int, int]]] = []

    def begin(index: int) -> None:
        nonlocal token_start
        if token_start is None:
            token_start = index

    def flush(end: int) -> None:
        nonlocal quoted, heredoc_operator, token_start
        if current or quoted:
            value = "".join(current)
            tokens.append(ShellToken(value, quoted=quoted))
            spans.append((end if token_start is None else token_start, end))
            if heredoc_operator is not None:
                pending_heredocs.append(
                    _PendingHeredoc(
                        value,
                        strip_tabs=heredoc_operator == "<<-",
                        quoted=quoted,
                        opener=len(tokens) - 1,
                    )
                )
                heredoc_operator = None
            # Follow the word with its substitutions' redirects so the segment
            # reports their targets while the word itself stays whole.
            for token, span in quoted_substitution_redirects:
                tokens.append(token)
                spans.append(span)
            quoted_substitution_redirects.clear()
            current.clear()
            quoted = False
        token_start = None

    index = 0
    while index < len(command):
        char = command[index]

        if in_single_quote:
            if ansi_c and char == "\\" and index + 1 < len(command):
                decoded, index = decode_ansi_c_escape(command, index, dialect=dialect)
                current.append(decoded)
                continue
            if char == "'":
                in_single_quote = False
            else:
                current.append(char)
            index += 1
            continue

        if in_double_quote:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\":
                if command.startswith("\\\n", index):
                    index += 2
                    continue
                quoted = True
                escaped = True
            elif char == '"':
                in_double_quote = False
            elif (close := _quoted_substitution_close(command, index)) is not None:
                # Bash runs a substitution inside double quotes too; its body is
                # scanned on its own so heredocs and newlines keep the word whole.
                body_start = (
                    index + 1 if char == "`" else skip_line_continuations(command, index + 1) + 1
                )
                quoted_substitution_redirects.extend(
                    _substitution_output_redirects(command, body_start, close)
                )
                current.append(command[index : close + 1])
                index = close + 1
                continue
            else:
                current.append(char)
            index += 1
            continue

        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue

        if char in " \t":
            flush(index)
            spaces = _PLAIN_SPACE_RUN.match(command, index)
            assert spaces is not None
            index = spaces.end()
            continue

        pattern_end = cases.pattern_ends.get(0)
        pattern_paren = False
        if token_start is None or char in "();&|\n" or index == pattern_end:
            pattern_paren = cases.pattern_paren(command, index, 0)
        if pattern_paren and char == ")" and index == pattern_end:
            arm_terminator = index
        if char == "(" and arm_terminator == index - 1:
            # A case pattern's closing parenthesis ends the word even when
            # the arm's subshell is glued to it. Quotes/expansions are data.
            flush(index)

        if char == "#" and token_start is None:
            # An unquoted word-initial ``#`` comments out the rest of the line; a
            # quote inside it opens nothing. The newline still ends the command.
            end = command.find("\n", index)
            end = len(command) if end == -1 else end
            index = end
            continue

        if (plain := _PLAIN_WORD_RUN.match(command, index)) is not None:
            begin(index)
            current.extend(plain[0])
            index = plain.end()
            continue

        if char == "`":
            begin(index)
            end = _substitution_end(command, index + 1, True, 0) + 1
            current.append(command[index:end])
            index = end
            continue

        if char == "\\":
            if index + 1 < len(command) and command[index + 1] == "\n":
                if arm_terminator == index - 1:
                    arm_terminator = index + 1
                index += 2
                continue
            begin(index)
            quoted = True
            escaped = True
            index += 1
            continue

        if char == "'" or opens_ansi_c_quote(command, index):
            # ANSI-C `$'...'` decodes C escapes (gobby.hooks._ansi_c).
            begin(index)
            quoted = True
            in_single_quote = True
            ansi_c = char == "$"
            index += 2 if ansi_c else 1
            continue

        if char == '"':
            begin(index)
            quoted = True
            in_double_quote = True
            index += 1
            continue

        # Arithmetic ``((``/``$((`` and a ``[[`` test are one word, so their
        # ``<``, ``>``, ``&&`` and ``||`` stay comparisons, never operators.
        # A compound holding a command substitution is scanned normally, so the
        # substitution's redirects stay writes; only its own comparisons are masked.
        if index in comparison_operators:
            begin(index)
            quoted = True
            current.append(char)
            index += 1
            continue
        compound_end = _compound_word_end(command, index, word_start=not current and not quoted)
        if compound_end is not None:
            if cases.pattern_ends.get(0) == compound_end - 1:
                # A parenthesized pattern is consumed as one word. Advance
                # its syntax frame before scanning a glued executable group.
                cases.pattern_paren(command, compound_end - 1, 0)
                arm_terminator = compound_end - 1
            if not (
                char == "(" and not command.startswith("((", index)
            ) and _COMMAND_SUBSTITUTION.search(command, index + 1, compound_end):
                comparison_operators |= _compound_comparison_operators(command, index, compound_end)
            else:
                begin(index)
                current.append(command[index:compound_end])
                index = compound_end
                continue

        # Bash ends a word at an unquoted ``)`` or closing backtick; a redirect
        # target inside ``$(...)``, ``( ... )`` or `` `...` `` must not keep it.
        if char in ")`" and current and tokens and is_shell_output_redirection_token(tokens[-1]):
            flush(index)

        scanned_operator = _scan_unquoted_shell_operator(command, index)
        if scanned_operator:
            operator, raw_length = scanned_operator
            start = index
            value = operator
            # Only a whole unquoted digit word adjacent to < or > is an IO_NUMBER.
            if (
                operator[0] in "<>"
                and joined_prefix_end(command, index, operator[0] + "(") is None
                and current
                and not quoted
                and heredoc_operator is None
                and all(char in "0123456789" for char in current)
            ):
                start = token_start if token_start is not None else index
                # Keep an incomplete fd duplication split and fail-closed as a write.
                if operator == ">&":
                    operator = ">"
                    raw_length = 1
                value = "".join(current) + operator
                current.clear()
                token_start = None
            else:
                flush(index)
            tokens.append(ShellToken(value))
            spans.append((start, index + raw_length))
            if operator == "\n":
                if pending_heredocs:
                    index = _skip_heredoc_bodies(command, index + 1, pending_heredocs, heredocs)
                    continue
            if operator in _HEREDOC_OPERATORS:
                heredoc_operator = operator
            index += raw_length
            continue

        if char.isspace():
            flush(index)
            index += 1
            continue

        begin(index)
        current.append(char)
        index += 1

    if in_single_quote or in_double_quote or escaped:
        raise ValueError("Unclosed shell quote or escape")

    flush(len(command))
    return ShellScan(tokens, spans, heredocs)


def _compound_word_end(command: str, index: int, *, word_start: bool) -> int | None:
    """Return the end of a subshell, arithmetic or ``[[`` compound at ``index``.

    ``$((`` may open anywhere in a word; ``((`` and ``[[ `` only at a word start.
    Returns None when nothing opens here or the construct never closes, so the
    caller falls back to ordinary word scanning.
    """
    if command[index] not in "$?*+@!([":
        return None
    if joined_prefix_end(command, index, "$((") is not None:
        return _balanced_parens_end(command, skip_line_continuations(command, index + 1))
    if command[index] in "?*+@!" and command.startswith("(", index + 1):
        return _balanced_parens_end(command, index + 1)
    if not word_start:
        return None
    if command.startswith("(", index) and not command.startswith("((", index):
        return _substitution_end(command, index + 1, False, 0) + 1
    if command.startswith("((", index):
        return _balanced_parens_end(command, index)
    if command.startswith("[[", index) and command[index + 2 : index + 3].isspace():
        return _double_bracket_end(command, index + 2)
    return None


def _quoted_substitution_close(command: str, index: int) -> int | None:
    """Index of the ``)`` or backtick closing a command substitution at ``index``.

    Arithmetic ``$((`` is not a substitution. Returns None when nothing opens
    here or the substitution never closes; Bash would not run it either.
    """
    if not _COMMAND_SUBSTITUTION.match(command, index):
        return None
    backtick = command[index] == "`"
    try:
        start = index + 1 if backtick else skip_line_continuations(command, index + 1) + 1
        return _substitution_end(command, start, backtick, 0)
    except ValueError:
        return None


def _substitution_output_redirects(
    command: str, start: int, end: int
) -> list[tuple[ShellToken, tuple[int, int]]]:
    """Output redirect operators and targets of ``command[start:end]``, spans rebased."""
    try:
        scan = scan_shell_command(command[start:end])
    except ValueError:
        return []
    redirects: list[tuple[ShellToken, tuple[int, int]]] = []
    for position, token in enumerate(scan.tokens[:-1]):
        if is_shell_output_redirection_token(token):
            for offset in (position, position + 1):
                span_start, span_end = scan.spans[offset]
                redirects.append((scan.tokens[offset], (start + span_start, start + span_end)))
    return redirects


def backtick_body(text: str) -> str:
    """Remove one backquote escape layer before inspecting its executable body."""
    return re.sub(r"\\([$`\\\n])", lambda match: "" if match[1] == "\n" else match[1], text)


def _substitution_end(text: str, start: int, backtick: bool, depth: int) -> int:
    if not 128 <= len(text) <= 32768:
        return _find_substitution_end(text, start, backtick, depth)
    return _cached_substitution_end(text, start, backtick, depth)


@lru_cache(maxsize=64)
def _cached_substitution_end(text: str, start: int, backtick: bool, depth: int) -> int:
    return _find_substitution_end(text, start, backtick, depth)


def _find_substitution_end(text: str, start: int, backtick: bool, depth: int) -> int:
    """Find the end of a literal substitution, respecting nested shell quotes.

    Heredoc bodies are data, so their quotes and parentheses never close it.
    """
    if depth > _MAX_SUBSTITUTION_DEPTH:
        raise ValueError("Shell nesting limit")
    if not backtick and (plain := _PLAIN_COMMAND.match(text, start)) is not None:
        prefix = plain[0]
        if re.search(r"\b(?:case|esac)\b", prefix) is None:
            # Without quotes, expansions, comments or case patterns, only the
            # parenthesis balance can close this body. Scan it in token runs.
            balance = 1
            for paren in re.finditer(r"[()]", prefix):
                balance += 1 if paren[0] == "(" else -1
                if balance == 0:
                    return start + paren.start()
    quote = ""
    parameter_quotes: list[str] = []
    level = 1
    # In arithmetic $((...)) ``<<`` is a shift, and a newline never starts a body.
    arithmetic = not backtick and text.startswith("(", start)
    cases = CaseSyntax(backtick=backtick)
    # The current logical line, masked like _prepare output, so the newline that
    # ends it can find the heredocs it opens.
    line: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if backtick and char == "`":
            return index
        pattern_paren = (
            not quote
            and not arithmetic
            and not parameter_quotes
            and (
                char in ";&|(){}\n"
                or index == start
                or text[index - 1].isspace()
                or text[index - 1] in ";&|(){}"
            )
            and cases.pattern_paren(text, index, level)
        )
        if (plain := _PLAIN_WORD_RUN.match(text, index)) is not None:
            line.append(plain[0])
            index = plain.end()
            continue
        if char == "\\" and (backtick or quote != "'"):
            line.append(text[index : index + 2])
            index += 2
            continue
        if not quote and opens_ansi_c_quote(text, index):
            quote = "$'"
            line.append("$'")
            index += 2
            continue
        if quote in {"'", "$'"}:
            if char == "'":
                quote = ""
        elif (pid_end := pid_parameter_end(text, index)) is not None:
            line.append(text[index:pid_end])
            index = pid_end
            continue
        elif (opening := joined_prefix_end(text, index, "$(")) is not None or char == "`":
            tick = char == "`"
            start_body = index + 1 if tick else opening
            assert start_body is not None
            index = _substitution_end(text, start_body, tick, depth + 1) + 1
            line.append("__gobby_expansion__")
            continue
        elif text.startswith("${", index):
            parameter_quotes.append(quote)
            quote = ""
            line.append("${")
            index += 2
            continue
        elif parameter_quotes and not quote and char == "}":
            quote = parameter_quotes.pop()
        elif char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif (
            not quote
            and not parameter_quotes
            and char == "#"
            and (index == start or text[index - 1].isspace() or text[index - 1] in ";|&(")
        ):
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
            if backtick:
                closing = text.find("`", index, end)
                end = end if closing == -1 else closing
            line.append(text[index:end])
            index = end
            continue
        elif not quote and not arithmetic and not parameter_quotes and char == "\n":
            tokens = scan_shell_command("".join(line)).tokens
            index = _skip_heredocs(text, tokens, index + 1)
            line = []
            continue
        elif (
            not quote
            and not backtick
            and not parameter_quotes
            and char in "()"
            and not pattern_paren
        ):
            level += 1 if char == "(" else -1
            if level == 0:
                return index
            char = ";"
        line.append(char)
        index += 1
    raise ValueError("Unclosed shell substitution")


def _skip_heredocs(command: str, tokens: list[ShellToken], index: int) -> int:
    """Return the index past the bodies of the heredocs ``tokens`` open, starting at ``index``."""
    for offset, token in enumerate(tokens[:-1]):
        if token.quoted or token.value.lstrip("0123456789") not in {"<<", "<<-"}:
            continue
        delimiter = tokens[offset + 1].value
        while index < len(command):
            end = command.find("\n", index)
            end = len(command) if end < 0 else end + 1
            body_line = command[index:end].rstrip("\n")
            index = end
            if token.value.lstrip("0123456789") == "<<-":
                body_line = body_line.lstrip("\t")
            if body_line == delimiter:
                break
    return index


def _double_bracket_end(command: str, index: int) -> int | None:
    quote = ""
    position = index
    while position < len(command):
        char = command[position]
        if char == "\\" and quote != "'":
            position += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif (
            command.startswith("]]", position)
            and command[position - 1].isspace()
            and (position + 2 == len(command) or command[position + 2] in " \t\n;&|)")
        ):
            return position + 2
        position += 1
    return None


def _skip_heredoc_bodies(
    command: str,
    index: int,
    pending_heredocs: list[_PendingHeredoc],
    heredocs: list[HeredocBody],
) -> int:
    """Advance past heredoc body lines, consuming pending delimiters in order.

    An unterminated heredoc swallows the rest of the command, matching how the
    shell would refuse to execute anything after it; the swallowed text is
    still recorded, unterminated, so callers can treat it as live input.
    """
    body_lines: list[str] = []
    body_start = index
    while pending_heredocs and index < len(command):
        pending = pending_heredocs[0]
        line_end = command.find("\n", index)
        if line_end == -1:
            line_end = len(command)
            next_index = line_end
        else:
            next_index = line_end + 1
        line = command[index:line_end]
        stripped = line.lstrip("\t") if pending.strip_tabs else line
        if stripped == pending.delimiter:
            pending_heredocs.pop(0)
            heredocs.append(
                HeredocBody(
                    "\n".join(body_lines),
                    quoted=pending.quoted,
                    terminated=True,
                    opener=pending.opener,
                    start=body_start,
                    end=next_index,
                )
            )
            body_lines = []
            body_start = next_index
        else:
            body_lines.append(stripped)
        index = next_index
    if pending_heredocs:
        pending = pending_heredocs[0]
        pending_heredocs.clear()
        heredocs.append(
            HeredocBody(
                "\n".join(body_lines),
                quoted=pending.quoted,
                terminated=False,
                opener=pending.opener,
                start=body_start,
                end=index,
            )
        )
    return index


def extract_heredoc_bodies(command: str) -> list[str]:
    """Return the body text of each terminated heredoc in ``command``."""
    bodies: list[str] = []
    try:
        tokenize_shell_command(command, heredoc_bodies=bodies)
    except ValueError:
        return []
    return bodies


def shell_token_values(tokens: list[ShellToken]) -> list[str]:
    return [token.value for token in tokens]


def _is_env_assignment(part: str) -> bool:
    name, separator, _value = part.partition("=")
    return bool(
        separator
        and name
        and (name[0].isalpha() or name[0] == "_")
        and all(char.isalnum() or char == "_" for char in name)
    )


def _strip_shell_wrappers(parts: list[str]) -> list[str]:
    """Drop env assignments and transparent prefixes ahead of a segment's command."""
    return strip_shell_wrappers(parts)


def is_fd_duplication_token(token: ShellToken) -> bool:
    """Return True for unquoted fd-duplication operators like ``2>&1`` or ``<&3``."""
    return not token.quoted and _FD_DUP_TOKEN_RE.match(token.value) is not None


def is_unquoted_shell_control_token(token: ShellToken) -> bool:
    return not token.quoted and (
        token.value in _SHELL_CONTROL_TOKENS
        or is_shell_input_redirection_token(token)
        or _FD_OUTPUT_REDIRECTION_RE.match(token.value) is not None
        or is_fd_duplication_token(token)
    )


def is_shell_input_redirection_token(token: ShellToken) -> bool:
    return not token.quoted and token.value.lstrip("0123456789") in _SHELL_INPUT_REDIRECTION_TOKENS


def is_shell_output_redirection_token(token: ShellToken) -> bool:
    return not token.quoted and (
        token.value in _SHELL_OUTPUT_REDIRECTION_TOKENS
        or _FD_OUTPUT_REDIRECTION_RE.match(token.value) is not None
    )


def has_shell_input_redirection(tokens: list[ShellToken]) -> bool:
    return any(is_shell_input_redirection_token(token) for token in tokens)


def strip_input_redirections(tokens: list[ShellToken]) -> list[ShellToken]:
    """Drop input-redirection operators and their immediate operands from tokens.

    Heredoc delimiters and redirected input files never name the command's own
    operands, so an interpreter with nothing left after the strip reads its
    program from stdin.
    """
    stripped: list[ShellToken] = []
    skip_next = False
    for idx, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if is_shell_input_redirection_token(token):
            if idx + 1 < len(tokens) and not is_unquoted_shell_control_token(tokens[idx + 1]):
                skip_next = True
            continue
        stripped.append(token)
    return stripped


def strip_output_redirections(tokens: list[ShellToken]) -> list[ShellToken]:
    """Drop output-redirection operators and their immediate targets from tokens.

    Fd-duplication operators are dropped too; they carry no target argument.
    """
    stripped: list[ShellToken] = []
    skip_next = False
    for idx, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if is_fd_duplication_token(token):
            continue
        if is_shell_output_redirection_token(token):
            if idx + 1 < len(tokens) and not is_unquoted_shell_control_token(tokens[idx + 1]):
                skip_next = True
            continue
        stripped.append(token)
    return stripped


def is_shell_tool(tool_name: Any) -> bool:
    """Return True when ``tool_name`` represents shell command execution."""
    return isinstance(tool_name, str) and tool_name in _SHELL_TOOLS


def canonicalize_shell_tool_name(tool_name: Any) -> Any:
    """Normalize shell aliases to the canonical ``Bash`` tool name."""
    if is_shell_tool(tool_name):
        return "Bash"
    return tool_name


def _get_command_text(tool_input: Any) -> str | None:
    """Extract a shell command string from normalized tool input."""
    if not isinstance(tool_input, dict):
        return None

    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command

    cmd = tool_input.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        return cmd

    return None


def _literal_cd_target(parts: list[str]) -> str | None:
    """Return a single literal ``cd`` target, if the command proves one."""
    if not parts or parts[0].rsplit("/", 1)[-1] != "cd":
        return None
    positional = [part for part in parts[1:] if part and not part.startswith("-")]
    if len(positional) != 1:
        return None
    target = positional[0]
    return None if any(char in target for char in "$`*?[]{};") else target


def _join_cwd(cwd: str, path: str) -> str:
    joined = posixpath.normpath(posixpath.join(cwd, path))
    if cwd.startswith(UNRESOLVED_DIRECTORY) and not joined.startswith(UNRESOLVED_DIRECTORY):
        return UNRESOLVED_DIRECTORY
    return joined


def _rebase_shell_path(path: str, cwd: str | None) -> str:
    if not cwd or path.startswith(("/", "~")) or "://" in path:
        return path
    return _join_cwd(cwd, path)


def _quoted_tilde_word(value: str, raw: str) -> bool:
    """A quoted or escaped tilde prefix cannot prove home-directory scope."""
    return value.startswith("~") and any(char in raw.split("/", 1)[0] for char in "'\"\\")


def _rebase_shell_paths(paths: list[str], cwd: str | None) -> list[str]:
    return [_rebase_shell_path(path, cwd) for path in paths]


def _rebase_navigation_shell_paths(paths: list[str], cwd: str | None) -> list[str]:
    def replace_reference(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        if name == "PWD":
            return "."
        return os.environ.get(name, match.group(0))

    expanded = [
        _apply_cd(None, _KNOWN_NAVIGATION_SHELL_REFERENCE.sub(replace_reference, path))
        for path in paths
    ]
    return _rebase_shell_paths(expanded, cwd)


def _apply_cd(cwd: str | None, target: str) -> str:
    if target.startswith("~"):
        target = os.path.expanduser(target)
        if target.startswith("~"):
            return UNRESOLVED_DIRECTORY
    if target.startswith("/"):
        return posixpath.normpath(target)
    if not cwd:
        return posixpath.normpath(target)
    return _join_cwd(cwd, target)


def _shell_positional_args(parts: list[str]) -> list[str]:
    """Return non-option shell args, excluding obvious control operators."""
    return [
        part
        for part in parts[1:]
        if part and part not in _SHELL_CONTROL_TOKENS and not part.startswith("-")
    ]


def _looks_file_like(candidate: str) -> bool:
    """Return True when ``candidate`` looks like a file path, not an inline script.

    Used to gate sed/awk's last positional arg so we don't classify an inline
    script (``'s/foo/bar/'``, ``'{print $1}'``) as a file that was read.
    """
    if not candidate or any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    # Must carry a path separator or an extension-like dot that isn't a leading/solo dot.
    if "/" in candidate:
        return True
    if "." in candidate and candidate not in {".", ".."}:
        return True
    return False


def _looks_path_target(candidate: str) -> bool:
    """Return True when ``candidate`` is a plausible shell path target."""
    if not candidate or candidate in _SHELL_CONTROL_TOKENS or candidate == "-":
        return False
    if candidate.startswith("-") or candidate.startswith("&"):
        return False
    if _contains_unexpanded_shell_reference(candidate):
        return True
    if any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    return True


def _contains_unexpanded_shell_reference(path: str) -> bool:
    """Return whether ``path`` still contains a runtime shell expansion."""
    return _UNEXPANDED_SHELL_REFERENCE.search(path) is not None


def is_dynamic_program_word(word: str) -> bool:
    """Recognize opaque command names while excluding compound comparisons."""
    return (
        not word.startswith("(")
        and not re.match(r"\[\[\s", word)
        and (_contains_unexpanded_shell_reference(word) or "`" in word)
    )


def _input_redirection_paths(tokens: list[ShellToken]) -> list[str]:
    """Return literal-looking paths read through stdin redirection."""
    paths: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if not is_shell_input_redirection_token(token) or token.value.lstrip("0123456789") != "<":
            continue
        candidate = tokens[index + 1]
        if is_unquoted_shell_control_token(candidate):
            continue
        if _looks_path_target(candidate.value) and candidate.value not in paths:
            paths.append(candidate.value)
    return paths


def _has_perl_inplace_option(parts: list[str]) -> bool:
    """Return True when a perl command edits files in place."""
    for part in parts[1:]:
        if part == "-pi" or part.startswith("-pi"):
            return True
        if part == "-i" or part.startswith("-i"):
            return True
    return False
