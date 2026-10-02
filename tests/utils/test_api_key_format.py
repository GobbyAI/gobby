"""API key format: the shared vectors D1's Rust mirror must reproduce."""

from __future__ import annotations

import pytest

from gobby.utils import api_key_format

pytestmark = pytest.mark.unit

# (32-byte secret hex, key, SHA-256 hex of the key, hint)
SHARED_VECTORS = [
    (
        "00" * 32,
        "gobby_00000000000000000000000000000000000000000002CZclj",
        "a9f63698d531f354cfaa4434bab6790e56968528916f7a1be277d0d8d300b707",
        "0000",
    ),
    (
        "ff" * 32,
        "gobby_yhjskwdA6OZ1AL1YmHWZWm8LLG7HjnuCA2j5rOw8Xp13sRzl1",
        "52387dbd37befc4de42e256ed58cc5ac1f678918547901b6a22f79c18b92ea65",
        "8Xp1",
    ),
    (
        bytes(range(32)).hex(),
        "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP",
        "5b285b61a8eb6a6c67f270bdf5cc6334bf9d0c63fe0fade7e744469d60f0c974",
        "IDlf",
    ),
]


def test_shared_vectors_and_rejections() -> None:
    for secret_hex, key, digest, hint in SHARED_VECTORS:
        assert api_key_format.encode(bytes.fromhex(secret_hex)) == key
        assert api_key_format.parse(key) == key
        assert api_key_format.hash(key) == digest
        assert api_key_format.hint(key) == hint

    generated = api_key_format.generate()
    assert api_key_format.parse(generated) == generated
    assert len(generated) == 55
    assert generated != api_key_format.generate()

    valid = SHARED_VECTORS[2][1]
    bad_checksum = valid[:-1] + ("Q" if valid[-1] != "Q" else "R")
    bad_alphabet = valid[:10] + "-" + valid[11:]
    rejected = [
        bad_checksum,
        bad_alphabet,
        valid[:-1],
        valid + "0",
        "gobbi_" + valid[6:],
        "",
    ]
    for key in rejected:
        assert api_key_format.parse(key) is None, key
