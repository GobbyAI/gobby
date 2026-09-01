"""Shell tool identity and command-token helpers."""

import re
from dataclasses import dataclass
from typing import Any

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


def scan_shell_command(command: str) -> ShellScan:
    """Tokenize ``command`` and keep each token's source span and heredoc bodies.

    Spans index into ``command`` with quotes and escapes included, so a token
    range maps back to its raw text. Raises ``ValueError`` on an unclosed
    quote or a trailing escape.
    """
    tokens: list[ShellToken] = []
    spans: list[tuple[int, int]] = []
    heredocs: list[HeredocBody] = []
    current: list[str] = []
    quoted = False
    in_single_quote = False
    in_double_quote = False
    escaped = False
    token_start: int | None = None
    pending_heredocs: list[_PendingHeredoc] = []
    heredoc_operator: str | None = None
    logical_continuation = False

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
            current.clear()
            quoted = False
        token_start = None

    index = 0
    while index < len(command):
        char = command[index]

        if in_single_quote:
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
            else:
                current.append(char)
            index += 1
            continue

        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue

        if char == "\\":
            if index + 1 < len(command) and command[index + 1] == "\n":
                index += 2
                continue
            begin(index)
            quoted = True
            escaped = True
            index += 1
            continue

        if char == "'":
            begin(index)
            quoted = True
            in_single_quote = True
            index += 1
            continue

        if char == '"':
            begin(index)
            quoted = True
            in_double_quote = True
            index += 1
            continue

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
        if stripped[:1] == ["command"]:
            stripped = stripped[1:]
            continue
        if stripped[:1] == ["env"]:
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
    if any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    return True


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
