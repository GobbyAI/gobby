"""task_requires_tdd tolerates the None list columns stored tasks carry."""

import pytest

from gobby.tasks.tdd_evidence import TDD_REQUIRED_LABEL, TDD_SKILL, task_requires_tdd

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("labels", "additional_skills", "enforce_tdd", "expected"),
    [
        (None, None, False, False),
        (None, None, True, True),
        ([TDD_REQUIRED_LABEL], None, False, True),
        (None, [TDD_SKILL], False, True),
    ],
)
def test_task_requires_tdd_accepts_unset_list_columns(
    labels: list[str] | None,
    additional_skills: list[str] | None,
    enforce_tdd: bool,
    expected: bool,
) -> None:
    """A stored task with NULL labels or skills must not crash the write gate (#24766)."""
    assert (
        task_requires_tdd(
            labels=labels,
            additional_skills=additional_skills,
            validation_criteria=None,
            enforce_tdd=enforce_tdd,
        )
        is expected
    )
