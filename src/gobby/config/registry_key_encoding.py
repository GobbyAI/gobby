"""Canonical UTF-8 encoding for dynamic configuration key segments."""

from __future__ import annotations

_SAFE_SEGMENT_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~"
)
_UPPER_HEX = frozenset("0123456789ABCDEF")

DYNAMIC_SEGMENT_CODEC_VECTORS: tuple[tuple[str, str], ...] = (
    ("plain", "plain"),
    ("AZaz09-_~", "AZaz09-_~"),
    ("dot.segment", "dot%2Esegment"),
    ("percent%sign", "percent%25sign"),
    ("already%2Eencoded", "already%252Eencoded"),
    ("slash/value", "slash%2Fvalue"),
    ("space value", "space%20value"),
    ("child.looking", "child%2Elooking"),
    ("café", "caf%C3%A9"),
    ("配置", "%E9%85%8D%E7%BD%AE"),
    ("🙂", "%F0%9F%99%82"),
    ("é", "%C3%A9"),
    ("e\u0301", "e%CC%81"),
)

INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS: tuple[str, ...] = ("\ud800",)

INVALID_DYNAMIC_SEGMENTS: tuple[str, ...] = (
    "",
    "%",
    "%2",
    "%GG",
    "%2e",
    "%41",
    "%7E",
    "raw.dot",
    "raw space",
    "bad+plus",
    "bad=equals",
    "%C0%AF",
    "%E0%80%AF",
    "%ED%A0%80",
    "é",
    "%FF",
)


def encode_dynamic_segment(value: str) -> str:
    """Encode one logical dynamic segment into its canonical UTF-8 form."""
    if not value:
        raise ValueError("Dynamic config segment must not be empty")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Dynamic config segment is not encodable UTF-8") from error
    encoded: list[str] = []
    for byte in raw:
        if byte in _SAFE_SEGMENT_BYTES:
            encoded.append(chr(byte))
        else:
            encoded.append(f"%{byte:02X}")
    return "".join(encoded)


def decode_dynamic_segment(value: str) -> str:
    """Decode a canonical segment and reject alternate or malformed spellings."""
    if not value:
        raise ValueError("Dynamic config segment must not be empty")
    decoded_bytes = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if index + 2 >= len(value):
                raise ValueError("Truncated percent escape in dynamic config segment")
            digits = value[index + 1 : index + 3]
            if any(digit not in _UPPER_HEX for digit in digits):
                raise ValueError("Percent escapes must use uppercase hexadecimal digits")
            decoded_bytes.append(int(digits, 16))
            index += 3
            continue
        byte = ord(character)
        if byte not in _SAFE_SEGMENT_BYTES:
            raise ValueError("Dynamic config segment is not canonically encoded")
        decoded_bytes.append(byte)
        index += 1
    try:
        decoded = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Dynamic config segment is not valid UTF-8") from error
    if encode_dynamic_segment(decoded) != value:
        raise ValueError("Dynamic config segment uses a noncanonical escape")
    return decoded
