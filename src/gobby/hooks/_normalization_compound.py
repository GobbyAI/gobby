"""Executable compound bodies from shell token spans."""

import re

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._case_syntax import case_pattern_end
from gobby.hooks._normalization_shell import ShellToken, _substitution_end, scan_shell_command

_FUNCTION_HEADER = re.compile(
    r"(?:(?:function\s+)?([^\s;&|()<>\"'\\$`]+)\s*\(\s*\)"
    r"|function\s+([^\s;&|()<>\"'\\$`]+))\s*"
)
_FUNCTION_BODY = re.compile(r"(?:[({]|(?:if|while|until|case|for|select)\b)")


def shell_function_bodies(
    command: str, tokens: list[ShellToken], spans: list[tuple[int, int]]
) -> tuple[str, dict[int, str]] | None:
    """Blank every header in place; map each body's opening position to its function name."""
    view: list[str] = []
    offsets: list[int] = []
    positions: dict[int, int] = {}
    quote = ""
    escaped = False
    word_start = True
    in_backtick = False
    heredoc_ends = (
        {body.start: body.end for body in scan_shell_command(command).heredocs}
        if "<<" in command
        else {}
    )
    index = 0
    while index < len(command):
        char = command[index]
        if not quote and not escaped and command.startswith("\\\n", index):
            index += 2
            continue
        end = heredoc_ends.get(index)
        if end is None and not quote and not escaped and char == "#" and word_start:
            end = command.find("\n", index)
            end = len(command) if end == -1 else end
            if in_backtick:
                close = command.find("`", index, end)
                end = end if close == -1 else close
        if end is not None:
            for original in range(index, end):
                positions[original] = len(view)
                offsets.append(original)
                view.append(command[original])
            index = end
            continue
        word_start = not quote and not escaped and (char.isspace() or char in ";&|()<>")
        if not quote and not escaped and char == "`":
            in_backtick = not in_backtick
        positions[index] = len(view)
        offsets.append(index)
        view.append(char)
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote[-1]:
                quote = ""
        elif char in "\"'":
            quote = "$'" if char == "'" and opens_ansi_c_quote(command, index - 1) else char
        index += 1
    offsets.append(len(command))
    normalized = "".join(view)
    headers: list[tuple[int, int]] = []
    bodies: dict[int, str] = {}
    for index in range(len(tokens)):
        start = spans[index][0]
        if headers and start < headers[-1][1]:
            continue
        header = _FUNCTION_HEADER.match(normalized, positions[start])
        if header is None:
            continue
        if _FUNCTION_BODY.match(normalized, header.end()):
            end = offsets[header.end()]
            headers.append((start, end))
            bodies[end] = header[1] or header[2]
    masked = list(command)
    for start, end in headers:
        masked[start:end] = " " * (end - start)
    program = "".join(masked)
    return (program, bodies) if headers else None


def heredoc_substitution_bodies(text: str, *, depth: int) -> tuple[list[str], bool]:
    """Extract expansions from an unquoted heredoc, where quotes are literal."""
    bodies: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if (text.startswith("$(", index) and not text.startswith("$((", index)) or char == "`":
            start = index + (1 if char == "`" else 2)
            try:
                end = _substitution_end(text, start, char == "`", depth + 1)
            except ValueError:
                return bodies, True
            bodies.append(text[start:end])
            index = end + 1
            continue
        index += 1
    return bodies, False


def command_substitution_bodies(
    text: str, spans: list[tuple[int, int]], *, depth: int
) -> tuple[str, list[tuple[int, str]]]:
    """Separate executed bodies before their operators can split the owner."""
    masked = list(text)
    bodies: list[tuple[int, str]] = []
    covered_until = 0
    for word_start, word_end in spans:
        quote = ""
        index = max(word_start, covered_until)
        while index < word_end:
            char = text[index]
            if char == "\\" and quote != "'":
                index += 2
                continue
            if not quote and opens_ansi_c_quote(text, index):
                quote = "$'"
                index += 2
                continue
            if quote not in {"'", "$'"} and (
                (text.startswith("$(", index) and not text.startswith("$((", index)) or char == "`"
            ):
                start = index + (1 if char == "`" else 2)
                end = _substitution_end(text, start, char == "`", depth + 1)
                bodies.append((index, text[start:end]))
                masked[start:end] = " " * (end - start)
                index = end + 1
                continue
            if char in "\"'":
                if not quote:
                    quote = char
                elif quote == char or (quote == "$'" and char == "'"):
                    quote = ""
            index += 1
        covered_until = max(covered_until, index)
    return "".join(masked), bodies


def process_substitution_bodies(
    tokens: list[ShellToken], spans: dict[int, tuple[int, int]], command: str
) -> tuple[list[ShellToken], list[str]]:
    owner: list[ShellToken] = []
    bodies: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.quoted and token.value in {"<", ">"} and index + 1 < len(tokens):
            following = tokens[index + 1]
            start, end = spans[id(following)]
            raw = command[start:end]
            if spans[id(token)][1] == start and raw.startswith("(") and raw.endswith(")"):
                bodies.append(raw[1:-1])
                index += 2
                continue
        owner.append(token)
        index += 1
    return owner, bodies


def case_pattern_process_bodies(
    tokens: list[ShellToken],
    kept: list[ShellToken],
    spans: dict[int, tuple[int, int]],
    command: str,
) -> tuple[list[str], bool]:
    """Extract process substitutions from parenthesized patterns removed from case arms."""
    kept_ids = {id(token) for token in kept}
    bodies: list[str] = []
    for token in tokens:
        if id(token) in kept_ids or token.quoted:
            continue
        start, end = spans[id(token)]
        raw = command[start:end]
        if not raw.startswith("(") or not raw.endswith(")"):
            continue
        inner = raw[1:-1]
        try:
            scan = scan_shell_command(inner)
        except ValueError:
            return bodies, True
        inner_spans = dict(zip((id(token) for token in scan.tokens), scan.spans, strict=True))
        bodies.extend(process_substitution_bodies(scan.tokens, inner_spans, inner)[1])
    return bodies, False


def case_arm_tokens(
    tokens: list[ShellToken],
    *,
    case_depth: int,
    new_arm: bool,
    command: str,
    spans: dict[int, tuple[int, int]],
    raw: dict[int, str] | None = None,
) -> tuple[list[ShellToken], int, bool]:
    """Remove case header/pattern syntax; keep each arm's executable tokens."""
    parts = list(tokens)
    pattern_start: int | None = None
    if parts and not parts[0].quoted and parts[0].value == "esac":
        return parts[1:], max(0, case_depth - 1), False
    if parts and not parts[0].quoted and parts[0].value == "case":
        case_depth += 1
        # The case word may itself be quoted; only the unquoted keyword opens arms.
        keyword = next(
            (
                index
                for index, token in enumerate(parts[2:], 2)
                if (not token.quoted and token.value == "in")
                or command[slice(*spans[id(token)])].startswith("in(")
            ),
            len(parts),
        )
        if keyword < len(parts) and command[slice(*spans[id(parts[keyword])])].startswith("in("):
            pattern_start = spans[id(parts[keyword])][0] + 2
            parts = parts[keyword:]
        else:
            parts = parts[keyword + 1 :]
        new_arm = True
    if case_depth and new_arm:
        if not parts:
            return [], case_depth, True
        start = (
            pattern_start
            if pattern_start is not None
            else min(spans[id(token)][0] for token in parts)
        )
        end = max(spans[id(token)][1] for token in parts)
        terminator = case_pattern_end(command, start, end)
        if terminator is None:
            return [], case_depth, True
        kept: list[ShellToken] = []
        for token in parts:
            word_start, word_end = spans[id(token)]
            if word_start > terminator:
                kept.append(token)
            elif word_end > terminator + 1:
                # Only the word crossing the boundary needs retokenization;
                # preserve the identities and quote provenance of other words.
                tail_start = terminator + 1
                try:
                    tail = scan_shell_command(command[tail_start:word_end])
                except ValueError:
                    # A glued group can span later words; rescan its complete
                    # body before deciding that the arm is malformed.
                    tail = scan_shell_command(command[tail_start:end])
                    for arm_token, (left, right) in zip(tail.tokens, tail.spans, strict=True):
                        spans[id(arm_token)] = (tail_start + left, tail_start + right)
                        if raw is not None:
                            raw[id(arm_token)] = command[tail_start + left : tail_start + right]
                        kept.append(arm_token)
                    return kept, case_depth, False
                for arm_token, (left, right) in zip(tail.tokens, tail.spans, strict=True):
                    spans[id(arm_token)] = (tail_start + left, tail_start + right)
                    if raw is not None:
                        raw[id(arm_token)] = command[tail_start + left : tail_start + right]
                    kept.append(arm_token)
        return kept, case_depth, False
    return parts, case_depth, False
