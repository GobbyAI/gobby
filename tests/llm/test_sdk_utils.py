"""Tests for additionalContext contributor truncation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.llm.sdk_utils import truncate_additional_context

pytestmark = pytest.mark.unit


def test_under_limit_joins_non_empty_contributors() -> None:
    text = truncate_additional_context(
        [("claimed_tasks", "claimed #1"), ("rule:empty", ""), ("skill:brevity", "be brief")],
        limit=100,
    )

    assert text == "claimed #1\n\nbe brief"


def test_oversized_single_contributor_keeps_siblings() -> None:
    text = truncate_additional_context(
        [
            ("claimed_tasks", "claimed #1"),
            ("agent_prompt", "p" * 200),
            ("skill:brevity", "be brief"),
        ],
        limit=100,
    )

    assert text == "claimed #1\n\nbe brief\n\nomitted contributors=[agent_prompt]"


def test_drops_largest_contributors_first_until_it_fits() -> None:
    text = truncate_additional_context(
        [("small", "s" * 10), ("largest", "l" * 60), ("middle", "m" * 50)],
        limit=80,
    )

    assert text == "s" * 10 + "\n\nomitted contributors=[largest,middle]"


def test_overflow_persists_full_text_and_names_result_id() -> None:
    store = MagicMock()
    store.save.return_value = "result-1"
    full_text = "keep\n\n" + "x" * 200

    text = truncate_additional_context(
        [("claimed_tasks", "keep"), ("rule:big", "x" * 200)],
        limit=120,
        session_id="session-1",
        project_id="project-1",
        store=store,
    )

    assert text == "keep\n\nomitted contributors=[rule:big]; get_tool_result result_id=result-1"
    store.save.assert_called_once_with(
        project_id="project-1",
        session_id="session-1",
        server_name="gobby-context",
        tool_name="additionalContext",
        content=full_text,
        content_kind="text",
        total_chars=len(full_text),
    )
