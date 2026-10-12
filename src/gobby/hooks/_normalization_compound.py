"""Executable case arms and process-substitution bodies from shell token spans."""

from gobby.hooks._ansi_c import opens_ansi_c_quote
from gobby.hooks._case_syntax import case_pattern_end
from gobby.hooks._normalization_shell import ShellToken, _substitution_end, scan_shell_command


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
                tail = scan_shell_command(command[tail_start:word_end])
                for arm_token, (left, right) in zip(tail.tokens, tail.spans, strict=True):
                    spans[id(arm_token)] = (tail_start + left, tail_start + right)
                    if raw is not None:
                        raw[id(arm_token)] = command[tail_start + left : tail_start + right]
                    kept.append(arm_token)
        return kept, case_depth, False
    return parts, case_depth, False
