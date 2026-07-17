"""Tests for structural session-summary validation."""

from __future__ import annotations

import pytest

from gobby.sessions.summary_validity import (
    is_summary_markdown_valid,
    summary_markdown_validation_error,
    summary_prompt_validation_error,
)

VALID_SUMMARY = """## Current State

The implementation is complete and the focused session tests pass. The task remains active
until its commit is linked through the configured lifecycle transition.

## Next Steps

Commit the verified changes and close the task with the resulting commit SHA.
"""


def test_accepts_substantive_structured_summary() -> None:
    assert is_summary_markdown_valid(VALID_SUMMARY) is True


@pytest.mark.parametrize(
    ("current_heading", "next_heading"),
    [
        ("# Current State", "###### Next Steps"),
        ("### current state:", "### NEXT STEPS:"),
        ("**Current State**", "__Next Steps__"),
        ("## **Current State:**", "## _Next Steps_:"),
    ],
)
def test_accepts_tolerated_semantic_heading_variants(
    current_heading: str,
    next_heading: str,
) -> None:
    summary = (
        f"{current_heading}\n\n"
        + ("Detailed implementation state. " * 5)
        + f"\n\n{next_heading}\n\n"
        + ("Continue with the verified handoff action. " * 4)
    )

    assert summary_markdown_validation_error(summary) is None
    assert is_summary_markdown_valid(summary) is True


@pytest.mark.parametrize(
    "summary",
    [
        None,
        "",
        "## Current State\n\nToo short.\n\n## Next Steps\n\nContinue.",
        "x" * 200,
        "## Current State\n\n" + ("Useful context. " * 10),
        "## Next Steps\n\n" + ("Continue the work. " * 10),
        "I'm sorry, but I cannot create that summary. " + ("provider refusal " * 10),
        "Internal Server Error\n\n## Current State\n\n"
        + ("garbage " * 20)
        + "\n\n## Next Steps\n\nRetry.",
    ],
)
def test_rejects_invalid_summary(summary: str | None) -> None:
    assert is_summary_markdown_valid(summary) is False


@pytest.mark.parametrize(
    ("summary", "expected_error"),
    [
        (None, "summary must be text"),
        ("Too short", "summary is shorter than 100 characters"),
        (
            "I'm sorry, but I cannot create that summary. " + ("provider refusal " * 10),
            "summary begins with a provider failure sentinel",
        ),
        (
            "## Current State\n\n" + ("Detailed state. " * 10),
            "summary is missing required section(s): Next Steps",
        ),
        (
            "## Next Steps\n\n" + ("Detailed action. " * 10),
            "summary is missing required section(s): Current State",
        ),
    ],
)
def test_reports_bounded_validation_reason(
    summary: str | None,
    expected_error: str,
) -> None:
    assert summary_markdown_validation_error(summary) == expected_error


def test_prompt_contract_requires_literal_mandatory_headings() -> None:
    assert (
        summary_prompt_validation_error(
            "Return exactly:\n## Current State\nDetails\n## Next Steps\nActions"
        )
        is None
    )
    assert summary_prompt_validation_error("Use **Current State** and **Next Steps**") == (
        "summary prompt must include literal required heading(s): ## Current State, ## Next Steps"
    )
