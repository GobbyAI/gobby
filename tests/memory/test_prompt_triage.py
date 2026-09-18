"""Tests for the cheap turn-start prompt triage."""

from __future__ import annotations

import pytest

from gobby.memory.prompt_triage import is_substantive_prompt

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("prompt", "substantive"),
    [
        ("", False),
        ("   \n  ", False),
        ("ok", False),
        ("Thanks!", False),
        ("sounds good", False),
        ("continue", False),
        ("please continue", False),
        ("go ahead", False),
        ("wait", False),
        ("hold on", False),
        ("status?", False),
        ("any progress", False),
        ("what's the status?", False),
        ("are you done?", False),
        ("/compact", False),
        ("/clear", False),
        ("please load the rust skill", False),
        ("stop the agent", False),
        ("close the task", False),
        ("resume the session", False),
        (
            "Add turn-start memory surfacing so the rule engine pushes a memory index "
            "into src/gobby/mcp_proxy/tools/memory_surface.py",
            True,
        ),
        ("Why does claim_task drop the title from its payload?", True),
        # The lifecycle skip is bounded at twelve words, so a real instruction that
        # opens with a lifecycle verb still earns a search.
        (
            "close the task once the migration lands and the isolated hub test run "
            "for tests/memory passes cleanly",
            True,
        ),
    ],
)
def test_triage_matrix(prompt: str, substantive: bool) -> None:
    assert is_substantive_prompt(prompt) is substantive
