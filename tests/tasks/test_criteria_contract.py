from __future__ import annotations

import pytest

from gobby.tasks.criteria_contract import split_validation_criteria

pytestmark = pytest.mark.unit


def test_list_criteria_discard_introductory_prose() -> None:
    criteria = split_validation_criteria(
        """
        Completion should be observable:

        - Focused unit tests pass.
        - The stored value is normalized.
          Continuation text remains attached.
        """
    )

    assert criteria == (
        "Focused unit tests pass.",
        "The stored value is normalized. Continuation text remains attached.",
    )


def test_non_list_criteria_preserve_prose_paragraphs() -> None:
    criteria = split_validation_criteria("First paragraph.\n\nSecond paragraph.")

    assert criteria == ("First paragraph.", "Second paragraph.")
