"""Tests for structural session-summary validation."""

from __future__ import annotations

import pytest

from gobby.sessions.summary_validity import is_summary_markdown_valid

VALID_SUMMARY = """## Current State

The implementation is complete and the focused session tests pass. The task remains active
until its commit is linked through the configured lifecycle transition.

## Next Steps

Commit the verified changes and close the task with the resulting commit SHA.
"""


def test_accepts_substantive_structured_summary() -> None:
    assert is_summary_markdown_valid(VALID_SUMMARY) is True


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
