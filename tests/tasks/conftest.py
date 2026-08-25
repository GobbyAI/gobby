"""Shared fixtures for task tests.

Mocks PromptLoader at the validation module level so tests don't
need a seeded database with prompt templates.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest


def _render_side_effect(path: str, context: dict) -> str:
    """Build a prompt string that includes all context variables.

    This ensures tests that check for specific content in the rendered
    prompt (e.g. file contents, criteria text) still pass.
    """
    parts = []
    for val in context.values():
        if val:
            parts.append(str(val))
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def mock_validation_prompt_loader():
    """Mock PromptLoader in validation module to avoid DB lookups."""
    with patch("gobby.tasks.validation.PromptLoader") as MockLoader:
        mock_instance = MagicMock()
        mock_instance.render.side_effect = _render_side_effect
        MockLoader.return_value = mock_instance
        yield mock_instance


@pytest.fixture(autouse=True)
def _isolated_evidence_snapshots() -> Iterator[None]:
    """Keep the per-session incremental derivation cache out of other tests."""
    from gobby.tasks.transcript_evidence import clear_evidence_snapshots

    clear_evidence_snapshots()
    yield
    clear_evidence_snapshots()
