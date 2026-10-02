"""Native semantic-frame conversion preserves style and rejects unproven cursors."""

from __future__ import annotations

import pytest

from gobby.agents.idle_detector import composer_text
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_frames import read_frame

pytestmark = pytest.mark.unit


def test_native_atomic_frame_keeps_draft_and_hides_faint_placeholder() -> None:
    cells = [
        {"symbol": symbol, "modifier": modifier, "skip": False}
        for symbol, modifier in [("❯", 0), (" ", 0), ("X", 0), ("?", 2), ("Y", 64)]
    ]
    frame = read_frame(
        {
            "type": "frame",
            "width": 5,
            "height": 1,
            "cells": cells,
            "cursor": {"x": 3, "y": 0, "visible": True},
        }
    )
    assert composer_text(frame.ansi).strip() == "❯ X Y"
    assert frame.cursor == (3, 0)


@pytest.mark.parametrize(
    "cursor", [None, {"x": 8, "y": 0, "visible": True}, {"x": 0, "y": 0, "visible": False}]
)
def test_unverified_native_cursor_refuses_proof(cursor: object) -> None:
    with pytest.raises(ProofRefused, match="cursor"):
        read_frame(
            {
                "type": "frame",
                "width": 1,
                "height": 1,
                "cells": [{"symbol": " ", "modifier": 0, "skip": False}],
                "cursor": cursor,
            }
        )
