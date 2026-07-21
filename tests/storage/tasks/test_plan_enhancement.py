from __future__ import annotations

import pytest

from gobby.storage.tasks._plan_enhancement import _fold_enhancement_round

pytestmark = pytest.mark.unit


def test_fold_enhancement_round_matches_only_complete_heading_lines() -> None:
    existing = "Planning notes mention ## Enhancement Suggestions — Round 2 inline."

    updated = _fold_enhancement_round(
        existing,
        2,
        ["Keep the inline text"],
        converged=False,
    )

    assert updated.startswith(existing)
    assert updated.count("## Enhancement Suggestions — Round 2") == 2


def test_fold_enhancement_round_stops_at_next_exact_heading_and_inserts_verbatim() -> None:
    existing = """## Enhancement Suggestions — Round 2

Old suggestion

## Enhancement Suggestions — Round 3 draft

This lookalike belongs to round 2.

## Enhancement Suggestions — Round 4

Keep round 4.
"""

    updated = _fold_enhancement_round(
        existing,
        2,
        [r"Preserve \g<1> and \1 literally"],
        converged=False,
    )

    assert r"Preserve \g<1> and \1 literally" in updated
    assert "Round 3 draft" not in updated
    assert "## Enhancement Suggestions — Round 4\n\nKeep round 4." in updated
