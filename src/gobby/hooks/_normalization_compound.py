"""Executable case arms and process-substitution bodies from shell token spans."""

from gobby.hooks._normalization_shell import ShellToken


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


def case_arm_tokens(
    tokens: list[ShellToken], *, case_depth: int, new_arm: bool
) -> tuple[list[ShellToken], int, bool]:
    """Remove case header/pattern syntax; keep each arm's executable tokens."""
    parts = list(tokens)
    if parts and not parts[0].quoted and parts[0].value == "esac":
        return parts[1:], max(0, case_depth - 1), False
    if parts and not parts[0].quoted and parts[0].value == "case":
        case_depth += 1
        # The case word may itself be quoted; only the unquoted keyword opens arms.
        keyword = next(
            (
                index
                for index, token in enumerate(parts[2:], 2)
                if not token.quoted and token.value == "in"
            ),
            len(parts),
        )
        parts = parts[keyword + 1 :]
        new_arm = True
    if case_depth and new_arm:
        end = next(
            (index for index, token in enumerate(parts) if token.value.endswith(")")),
            len(parts),
        )
        pending = end == len(parts)
        return parts[end + 1 :], case_depth, pending
    return parts, case_depth, False
