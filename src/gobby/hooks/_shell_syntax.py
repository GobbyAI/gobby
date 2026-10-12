"""Raw punctuation recognition after shell line-continuation removal.

Callers own quote and escape context. Offsets always refer to the original source.
"""

import re

_FD_DUP_SCAN_RE = re.compile(
    r"[<>](?:\\\n)*&(?:\\\n)*(?:\d(?:(?:\\\n)*\d)*|-)(?=(?:\\\n)*(?:$|[\s;&|<>]))"
)


def skip_line_continuations(text: str, index: int) -> int:
    while text.startswith("\\\n", index):
        index += 2
    return index


def joined_prefix_end(text: str, index: int, prefix: str) -> int | None:
    """Match punctuation across continuations; return its raw exclusive end."""
    for offset, expected in enumerate(prefix):
        if offset:
            index = skip_line_continuations(text, index)
        if index >= len(text) or text[index] != expected:
            return None
        index += 1
    return index


def _scan_unquoted_shell_operator(command: str, index: int) -> tuple[str, int] | None:
    char = command[index]
    if char == "\n":
        return "\n", 1
    if char not in ";&|<>":
        return None
    fd_dup = _FD_DUP_SCAN_RE.match(command, index)
    if fd_dup:
        raw = fd_dup.group(0)
        return raw.replace("\\\n", ""), len(raw)
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
        if (end := joined_prefix_end(command, index, operator)) is not None:
            return operator, end - index
    return None
