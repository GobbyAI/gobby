from __future__ import annotations

import pytest

from gobby.review_learning.guidance import (
    format_review_lesson_guidance,
    has_actionable_guidance,
)

pytestmark = pytest.mark.unit


def test_all_empty_lessons_do_not_render_guidance_block() -> None:
    lessons = [
        {
            "pattern_id": "empty-lesson",
            "do": " ",
            "avoid": "\t",
            "principle": "",
            "prevention": None,
        }
    ]

    assert has_actionable_guidance(lessons[0]) is False
    assert format_review_lesson_guidance(lessons) == ""


@pytest.mark.parametrize(
    ("field", "expected_label"),
    [
        ("do", "Do:"),
        ("avoid", "Avoid:"),
        ("principle", "Do:"),
        ("prevention", "Do:"),
    ],
)
def test_each_actionable_guidance_field_renders(field: str, expected_label: str) -> None:
    lesson = {
        "pattern_id": f"{field}-lesson",
        "matched_file_path": "src/gobby/example.py",
        field: f"{field} guidance.",
    }

    message = format_review_lesson_guidance([lesson])

    assert has_actionable_guidance(lesson) is True
    assert "<review-guidance>" in message
    assert expected_label in message
    assert f"{field} guidance" in message
