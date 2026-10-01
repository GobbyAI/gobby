"""Bash ANSI-C ``$'...'`` escape decoding for the shell scanner.

Inside ``$'...'`` bash decodes C escapes before the word runs, so ``\\n`` is a
real newline and ``\\x67`` a ``g``. A guard that rescans a wrapped script must
see the decoded text, or an escape spells a separator or command name it misses.
"""

from __future__ import annotations

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


def decode_ansi_c_escape(text: str, index: int) -> tuple[str, int]:
    """Decode the escape whose backslash is at ``index``; return it and the next index.

    A decoded NUL stays in the text: bash would cut the string there, but zsh
    keeps it and ``eval`` runs what follows. An escape bash does not recognise
    keeps its backslash.
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
    # The lexer pairs each backslash with the next character before decoding, so
    # `\c` never takes the closing quote, and `\c\X` keeps X from the `\X` pair.
    if kind == "c" and index + 2 < len(text) and text[index + 2] != "'":
        target = text[index + 2]
        if target == "\\":
            pair = text[index + 3 : index + 4]
            if pair:
                return "\x1c" + ("" if pair == "\\" else pair), index + 4
        else:
            # The mask clears the case bit, so `\ca` and `\cA` are both ^A.
            return chr(0x7F if target == "?" else ord(target) & 0x1F), index + 3
    return text[index : index + 2], index + 2


__all__ = ["decode_ansi_c_escape"]
