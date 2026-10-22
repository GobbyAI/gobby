"""task_requires_tdd tolerates the None list columns stored tasks carry."""

import pytest

from gobby.workflows.claimed_task_extra_skills import TDD_SKILL, task_requires_tdd

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("labels", "additional_skills", "expected"),
    [
        (None, None, False),
        (["tdd:required"], None, True),
        (None, [TDD_SKILL], True),
    ],
)
def test_task_requires_tdd_accepts_unset_list_columns(
    labels: list[str] | None,
    additional_skills: list[str] | None,
    expected: bool,
) -> None:
    """Stored NULL labels or skills must not crash skill selection."""
    assert (
        task_requires_tdd(
            labels=labels,
            additional_skills=additional_skills,
            validation_criteria=None,
        )
        is expected
    )
