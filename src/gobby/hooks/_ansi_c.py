"""ANSI-C ``$'...'`` escape decoding for the shell scanner.

Inside ``$'...'`` the shell decodes C escapes before the word runs, so ``\\n`` is a
real newline and ``\\x67`` a ``g``. A guard that rescans a wrapped script must
see the decoded text, or an escape spells a separator or command name it misses.

bash and zsh disagree on several escapes (``\\cX``, ``\\C-X``, ``\\M-X``, an unknown
``\\X``), so callers that guard execution decode with each dialect and check both.
"""

from __future__ import annotations

from typing import Literal

ShellDialect = Literal["bash", "zsh"]
SHELL_DIALECTS: tuple[ShellDialect, ...] = ("bash", "zsh")

_SIMPLE = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
_OCTAL = "01234567"
_HEX = "0123456789abcdefABCDEF"
# Hex digits each escape reads at most, as bash's ansicstr does.
_HEX_WIDTH = {"x": 2, "u": 4, "U": 8}


def _digits(text: str, start: int, alphabet: str, width: int) -> int:
    end = start
    while end < len(text) and end - start < width and text[end] in alphabet:
        end += 1
    return end


def _zsh_modified(text: str, index: int) -> tuple[str, int]:
    """Decode zsh's ``\\C-X`` (control) or ``\\M-X`` (meta); ``X`` may be an escape."""
    kind = text[index + 1]
    target = index + 2 + (text[index + 2 : index + 3] == "-")
    if target >= len(text) or text[target] == "'":
        return "", target
    if text[target] == "\\" and target + 1 < len(text):
        value, end = decode_ansi_c_escape(text, target, dialect="zsh")
    else:
        value, end = text[target], target + 1
    if not value:
        return "", end
    code = ord(value[0])
    # Control keeps the meta bit, so `\C-\M-a` and `\M-\C-a` are both 0x81.
    code = code | 0x80 if kind == "M" else 0x7F if value[0] == "?" else code & 0x9F
    return chr(code) + value[1:], end


def decode_ansi_c_escape(
    text: str, index: int, *, dialect: ShellDialect = "bash"
) -> tuple[str, int]:
    """Decode the escape whose backslash is at ``index``; return it and the next index.

    A decoded NUL stays in the text in both dialects: bash would cut the string
    there, but zsh keeps it and ``eval`` runs what follows. The closing quote is
    never consumed.
    """
    kind = text[index + 1 : index + 2]
    if kind in _SIMPLE:
        return _SIMPLE[kind], index + 2
    if kind and kind in _OCTAL:
        end = _digits(text, index + 1, _OCTAL, 3)
        return chr(int(text[index + 1 : end], 8) & 0xFF), end
    if kind in _HEX_WIDTH:
        end = _digits(text, index + 2, _HEX, _HEX_WIDTH[kind])
        if end > index + 2 and (value := int(text[index + 2 : end], 16)) <= 0x10FFFF:
            return chr(value), end
        if dialect == "zsh" and end == index + 2:
            # zsh reads an escape with no digits as NUL.
            return "\0", end
    if dialect == "zsh":
        if kind in {"C", "M"}:
            return _zsh_modified(text, index)
        # zsh drops the backslash of any other escape, `\c` included.
        return kind, index + 2
    if kind == "c":
        # The lexer pairs each backslash with the next character before decoding,
        # so `\c` before the closing quote reads as a lone backslash, and `\c\X`
        # is ^\ followed by X.
        target = text[index + 2 : index + 3]
        if not target or target == "'":
            return "\\", index + 2
        if target == "\\":
            return "\x1c" + text[index + 3 : index + 4], index + 4
        # The mask clears the case bit, so `\ca` and `\cA` are both ^A.
        return chr(0x7F if target == "?" else ord(target) & 0x1F), index + 3
    return text[index : index + 2], index + 2


__all__ = ["SHELL_DIALECTS", "ShellDialect", "decode_ansi_c_escape"]
