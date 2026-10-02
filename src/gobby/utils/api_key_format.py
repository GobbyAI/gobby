"""Gobby API key format.

A key is ``gobby_`` + 43 base62 chars encoding 32 random bytes (big-endian,
zero-padded) + 6 base62 chars of the zero-padded CRC32 of that 43-char body.
Keys are stored only as their SHA-256 hex digest. The vectors in
``tests/utils/test_api_key_format.py`` are the contract the Rust mirror matches.
"""

from __future__ import annotations

import secrets
import zlib

from gobby.storage.auth import hash_token

PREFIX = "gobby_"
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SECRET_BYTES = 32
BODY_LEN = 43
CHECKSUM_LEN = 6
KEY_LEN = len(PREFIX) + BODY_LEN + CHECKSUM_LEN
_ALPHABET_SET = frozenset(ALPHABET)


def _base62(value: int, width: int) -> str:
    digits: list[str] = []
    while value:
        value, digit = divmod(value, 62)
        digits.append(ALPHABET[digit])
    return "".join(reversed(digits)).rjust(width, "0")


def _checksum(body: str) -> str:
    return _base62(zlib.crc32(body.encode("ascii")), CHECKSUM_LEN)


def encode(secret: bytes) -> str:
    """Encode a 32-byte secret as a key."""
    if len(secret) != SECRET_BYTES:
        raise ValueError(f"API key secret must be {SECRET_BYTES} bytes")
    body = _base62(int.from_bytes(secret, "big"), BODY_LEN)
    return f"{PREFIX}{body}{_checksum(body)}"


def generate() -> str:
    """Return a new random key."""
    return encode(secrets.token_bytes(SECRET_BYTES))


def parse(key: str) -> str | None:
    """Return ``key`` when its prefix, length, alphabet and checksum are valid."""
    if len(key) != KEY_LEN or not key.startswith(PREFIX):
        return None
    rest = key[len(PREFIX) :]
    if not _ALPHABET_SET.issuperset(rest):
        return None
    body, checksum = rest[:BODY_LEN], rest[BODY_LEN:]
    if not secrets.compare_digest(checksum, _checksum(body)):
        return None
    return key


def hash(key: str) -> str:
    """Return the at-rest SHA-256 hex digest of a key."""
    return hash_token(key)


def hint(key: str) -> str:
    """Return the last four body chars, shown to identify a key."""
    return key[len(PREFIX) + BODY_LEN - 4 : len(PREFIX) + BODY_LEN]
