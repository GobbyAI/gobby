"""Shell tool identity and command-token helpers."""

import os
import posixpath
import re
from dataclasses import dataclass
from typing import Any

from gobby.hooks._ansi_c import ShellDialect, decode_ansi_c_escape
from gobby.hooks._normalization_paths import _append_unique_path

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
# The scan variant's lookahead keeps ``>&2file`` (bash: redirect to the *file*
# ``2file``) out of fd-dup territory: the fd digits must end at a delimiter.
_FD_DUP_SCAN_RE = re.compile(r"\d*[<>]&(?:\d+|-)(?=$|[\s;&|<>])")
_FD_DUP_TOKEN_RE = re.compile(r"^\d*[<>]&(?:\d+|-)$")

# Output sinks that never mutate files; redirecting to them is not a write.
_BENIGN_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

# Characters that strongly imply an inline sed/awk script rather than a file path.
_SCRIPT_LIKE_CHARS = frozenset({"{", "}", "$", ";", "(", ")"})
# `$` opening a variable (`$VAR`, `${VAR}`), command substitution (`$(cmd)`),
# positional parameter (`$1`), or special parameter — anything expanded at runtime.
_UNEXPANDED_SHELL_REFERENCE = re.compile(r"\$[\w{(@*?#$!-]")
# `$(cmd)` (but not arithmetic `$((`) or a backtick substitution: commands that run.
_COMMAND_SUBSTITUTION = re.compile(r"\$\((?!\()|`")
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
    in the owning scan, which places the body in its consumer's segment even
    when a pipeline continuation defers the body past later tokens.
    """

    text: str
    quoted: bool
    terminated: bool
    opener: int


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
    """Tokenize ``command`` and keep each token's source span and heredoc bodies.

    Spans index into ``command`` with quotes and escapes included, so a token
    range maps back to its raw text. Raises ``ValueError`` on an unclosed
    quote or a trailing escape. ``dialect`` selects how ``$'...'`` escapes decode.
    """
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
    logical_continuation = False
    comparison_operators: set[int] = set()
    in_backtick = False
    # Output redirects of substitutions inside the current double-quoted word.
    quoted_substitution_redirects: list[tuple[ShellToken, tuple[int, int]]] = []

    def begin(index: int) -> None:
        nonlocal token_start
        if token_start is None:
            token_start = index

    def flush(end: int) -> None:
        nonlocal quoted, heredoc_operator, token_start, logical_continuation
        if current or quoted:
            value = "".join(current)
            tokens.append(ShellToken(value, quoted=quoted))
            spans.append((end if token_start is None else token_start, end))
            # A word after ``|``/``&&``/``||`` completes the continuation; the
            # next newline ends the command and starts any pending heredoc body.
            logical_continuation = False
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
                quoted = True
                escaped = True
            elif char == '"':
                in_double_quote = False
            elif (close := _quoted_substitution_close(command, index)) is not None:
                # Bash runs a substitution inside double quotes too; its body is
                # scanned on its own so heredocs and newlines keep the word whole.
                body_start = index + (1 if char == "`" else 2)
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

        if char == "#" and token_start is None:
            # An unquoted word-initial ``#`` comments out the rest of the line; a
            # quote inside it opens nothing. The newline still ends the command,
            # and inside unquoted backticks so does the closing backtick.
            end = command.find("\n", index)
            end = len(command) if end == -1 else end
            if in_backtick:
                closing = command.find("`", index, end)
                end = end if closing == -1 else closing
            index = end
            continue

        if char == "`":
            in_backtick = not in_backtick

        if char == "\\":
            if index + 1 < len(command) and command[index + 1] == "\n":
                index += 2
                continue
            begin(index)
            quoted = True
            escaped = True
            index += 1
            continue

        if char == "'" or command.startswith("$'", index):
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
            if _COMMAND_SUBSTITUTION.search(command, index + 1, compound_end):
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

        operator = _scan_unquoted_shell_operator(command, index)
        if operator:
            flush(index)
            tokens.append(ShellToken(operator))
            spans.append((index, index + len(operator)))
            if operator == "\n":
                if pending_heredocs and not logical_continuation:
                    index = _skip_heredoc_bodies(command, index + 1, pending_heredocs, heredocs)
                    continue
                logical_continuation = False
            elif operator in {"&&", "||", "|"}:
                logical_continuation = True
            if operator in _HEREDOC_OPERATORS:
                heredoc_operator = operator
            index += len(operator)
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
    """Return the end of an arithmetic or ``[[`` compound word starting at ``index``.

    ``$((`` may open anywhere in a word; ``((`` and ``[[ `` only at a word start.
    Returns None when nothing opens here or the construct never closes, so the
    caller falls back to ordinary word scanning.
    """
    if command.startswith("$((", index):
        return _balanced_parens_end(command, index + 1)
    if not word_start:
        return None
    if command.startswith("((", index):
        return _balanced_parens_end(command, index)
    if command.startswith("[[", index) and command[index + 2 : index + 3].isspace():
        return _double_bracket_end(command, index + 2)
    return None


def _compound_comparison_operators(command: str, start: int, end: int) -> set[int]:
    """Indexes of ``<``, ``>``, ``&`` and ``|`` in a compound word outside quotes
    and command substitutions: the compound's own comparisons, never redirects.
    """
    positions: set[int] = set()
    quote = ""
    depth = 0
    in_backtick = False
    position = start
    while position < end:
        char = command[position]
        if char == "\\" and quote != "'":
            position += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "`":
            in_backtick = not in_backtick
        elif depth or in_backtick:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        elif command.startswith("$(", position) and not command.startswith("$((", position):
            depth = 1
            position += 2
            continue
        elif char in "<>&|":
            positions.add(position)
        position += 1
    return positions


def _balanced_parens_end(command: str, index: int) -> int | None:
    depth = 0
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
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
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
        return _substitution_end(command, index + (1 if backtick else 2), backtick, 0)
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


def _substitution_end(text: str, start: int, backtick: bool, depth: int) -> int:
    """Find the end of a literal substitution, respecting nested shell quotes.

    Heredoc bodies are data, so their quotes and parentheses never close it.
    """
    if depth > _MAX_SUBSTITUTION_DEPTH:
        raise ValueError("Shell nesting limit")
    quote = ""
    level = 1
    # In arithmetic $((...)) ``<<`` is a shift, and a newline never starts a body.
    arithmetic = not backtick and text.startswith("(", start)
    # The current logical line, masked like _prepare output, so the newline that
    # ends it can find the heredocs it opens.
    line: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            line.append(text[index : index + 2])
            index += 2
            continue
        if backtick and char == "`":
            return index
        if quote == "'":
            if char == quote:
                quote = ""
        elif text.startswith("$(", index) or char == "`":
            tick = char == "`"
            index = _substitution_end(text, index + (1 if tick else 2), tick, depth + 1) + 1
            line.append("__gobby_expansion__")
            continue
        elif char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif not quote and not arithmetic and char == "\n":
            tokens = scan_shell_command("".join(line)).tokens
            if not tokens or tokens[-1].value not in {"|", "&&", "||"}:
                index = _skip_heredocs(text, tokens, index + 1)
                line = []
                continue
            char = " "
        elif not quote and not backtick and char in "()":
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
        if token.quoted or token.value not in {"<<", "<<-"}:
            continue
        delimiter = tokens[offset + 1].value
        while index < len(command):
            end = command.find("\n", index)
            end = len(command) if end < 0 else end + 1
            body_line = command[index:end].rstrip("\n")
            index = end
            if token.value == "<<-":
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
                )
            )
            body_lines = []
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


def _scan_unquoted_shell_operator(command: str, index: int) -> str | None:
    char = command[index]
    if char == "\n":
        return "\n"
    # Every operator opens with one of these or a digit; the scanner probes
    # each character, so skip the regex and prefix checks for the rest.
    if char not in ";&|<>" and not char.isdigit():
        return None
    # Like the ``N>`` branch below, this fires on digits adjacent to a word
    # (``src2>&1`` scans as ``src`` + ``2>&1`` where bash reads ``src2`` + ``>&1``);
    # fd duplication is classification-neutral either way.
    fd_dup = _FD_DUP_SCAN_RE.match(command, index)
    if fd_dup:
        return fd_dup.group(0)
    if char.isdigit():
        cursor = index
        while cursor < len(command) and command[cursor].isdigit():
            cursor += 1
        if command.startswith(">>", cursor):
            return f"{command[index:cursor]}>>"
        if command.startswith(">", cursor):
            return f"{command[index:cursor]}>"
        return None
    for operator in (
        "<<<",
        "<<-",
        "&>>",
        "&&",
        "||",
        "<<",
        ">>",
        "&>",
        ";",
        "|",
        "&",
        "<",
        ">&",
        ">",
    ):
        if command.startswith(operator, index):
            return operator
    return None


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
    stripped = list(parts)
    while stripped:
        while stripped and _is_env_assignment(stripped[0]):
            stripped = stripped[1:]
        # A wrapper with options stays whole, or an option would read as the command.
        if stripped[:1] in (["command"], ["env"]) and not "".join(stripped[1:2]).startswith("-"):
            stripped = stripped[1:]
            continue
        # Loop/conditional body keywords prefix the real command after a
        # segment split (`do grep ...`, `then cat ...`); classify what follows.
        if stripped[:1] in (["do"], ["then"], ["else"]):
            stripped = stripped[1:]
            continue
        break
    return stripped


def is_fd_duplication_token(token: ShellToken) -> bool:
    """Return True for unquoted fd-duplication operators like ``2>&1`` or ``<&3``."""
    return not token.quoted and _FD_DUP_TOKEN_RE.match(token.value) is not None


def is_unquoted_shell_control_token(token: ShellToken) -> bool:
    return not token.quoted and (
        token.value in _SHELL_CONTROL_TOKENS
        or _FD_OUTPUT_REDIRECTION_RE.match(token.value) is not None
        or is_fd_duplication_token(token)
    )


def is_shell_input_redirection_token(token: ShellToken) -> bool:
    return not token.quoted and token.value in _SHELL_INPUT_REDIRECTION_TOKENS


def is_shell_output_redirection_token(token: ShellToken) -> bool:
    return not token.quoted and (
        token.value in _SHELL_OUTPUT_REDIRECTION_TOKENS
        or _FD_OUTPUT_REDIRECTION_RE.match(token.value) is not None
    )


def has_shell_input_redirection(tokens: list[ShellToken]) -> bool:
    return any(is_shell_input_redirection_token(token) for token in tokens)


def is_benign_redirect_target(path: str) -> bool:
    """Return True when redirecting output to ``path`` cannot mutate a file."""
    return path in _BENIGN_REDIRECT_TARGETS


def has_mutating_output_redirection(tokens: list[ShellToken]) -> bool:
    """Return True when any output redirection may write somewhere other than a benign sink.

    Fails closed: a redirection with a missing or undeterminable target counts
    as mutating.
    """
    for idx, token in enumerate(tokens):
        if not is_shell_output_redirection_token(token):
            continue
        if idx + 1 >= len(tokens):
            return True
        candidate = tokens[idx + 1]
        if is_unquoted_shell_control_token(candidate):
            return True
        if not is_benign_redirect_target(candidate.value):
            return True
    return False


def redirects_stdout_to_file(tokens: list[ShellToken]) -> bool:
    """Return True when an output redirection sends stdout to a file, not the model.

    Stderr-only and higher-fd redirections leave stdout visible; benign sinks
    are not file writes.
    """
    for idx, token in enumerate(tokens):
        if not is_shell_output_redirection_token(token):
            continue
        if token.value in {"2>", "2>>"} or (
            _FD_OUTPUT_REDIRECTION_RE.match(token.value) is not None
            and not token.value.startswith("1")
        ):
            continue
        if idx + 1 >= len(tokens):
            continue
        target = tokens[idx + 1]
        if is_unquoted_shell_control_token(target):
            continue
        if not is_benign_redirect_target(target.value):
            return True
    return False


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


def extract_redirection_paths(tokens: list[ShellToken]) -> list[str]:
    """Extract explicit output redirection targets from quote-aware shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(tokens[:-1]):
        if not is_shell_output_redirection_token(token):
            continue
        candidate = tokens[idx + 1]
        if is_unquoted_shell_control_token(candidate):
            continue
        if is_benign_redirect_target(candidate.value):
            continue
        if _looks_path_target(candidate.value):
            _append_unique_path(paths, candidate.value)
    return paths


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


def _rebase_shell_path(path: str, cwd: str | None) -> str:
    if not cwd or path.startswith(("/", "~")) or "://" in path:
        return path
    return posixpath.normpath(posixpath.join(cwd, path))


def _rebase_shell_paths(paths: list[str], cwd: str | None) -> list[str]:
    return [_rebase_shell_path(path, cwd) for path in paths]


def _rebase_navigation_shell_paths(paths: list[str], cwd: str | None) -> list[str]:
    def replace_reference(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        if name == "PWD":
            return "."
        return os.environ.get(name, match.group(0))

    expanded = [
        posixpath.normpath(_KNOWN_NAVIGATION_SHELL_REFERENCE.sub(replace_reference, path))
        for path in paths
    ]
    return _rebase_shell_paths(expanded, cwd)


def _apply_cd(cwd: str | None, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target)
    if not cwd:
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(cwd, target))


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


def _input_redirection_paths(tokens: list[ShellToken]) -> list[str]:
    """Return literal-looking paths read through stdin redirection."""
    paths: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if not is_shell_input_redirection_token(token) or token.value != "<":
            continue
        candidate = tokens[index + 1]
        if is_unquoted_shell_control_token(candidate):
            continue
        if _looks_path_target(candidate.value) and candidate.value not in paths:
            paths.append(candidate.value)
    return paths


def _has_sed_inplace_option(parts: list[str]) -> bool:
    """Return True when a sed command performs in-place editing."""
    for part in parts[1:]:
        if part in {"-i", "--in-place"}:
            return True
        if part.startswith("-i"):
            return True
        if part.startswith("--in-place="):
            return True
    return False


def _has_perl_inplace_option(parts: list[str]) -> bool:
    """Return True when a perl command edits files in place."""
    for part in parts[1:]:
        if part == "-pi" or part.startswith("-pi"):
            return True
        if part == "-i" or part.startswith("-i"):
            return True
    return False


def _extract_redirection_paths(parts: list[str]) -> list[str]:
    """Extract explicit output redirection targets from shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(parts[:-1]):
        if token not in _SHELL_OUTPUT_REDIRECTION_TOKENS and not _FD_OUTPUT_REDIRECTION_RE.match(
            token
        ):
            continue
        candidate = parts[idx + 1]
        if is_benign_redirect_target(candidate):
            continue
        if _looks_path_target(candidate):
            _append_unique_path(paths, candidate)
    return paths
