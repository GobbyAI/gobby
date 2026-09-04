from __future__ import annotations

from pathlib import Path

import pytest

from gobby.terminals.frame_client import decode_frame

pytestmark = pytest.mark.unit

WIRE_GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gterminal"
    / "tests"
    / "fixtures"
    / "wire_golden"
)


def test_semantic_frame_raw_is_retained() -> None:
    framed = (WIRE_GOLDEN / "frame.bin").read_bytes()
    payload = framed[4:]

    decoded = decode_frame(framed)

    assert decoded["type"] == "frame"
    assert decoded["raw"] == payload
    assert decode_frame(len(payload).to_bytes(4, "little") + decoded["raw"]) == decoded
