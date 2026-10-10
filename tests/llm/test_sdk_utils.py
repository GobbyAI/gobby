"""Tests for additionalContext contributor truncation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.llm.sdk_utils import fit_additional_context, truncate_additional_context

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


def test_agent_prompt_drops_after_every_other_part() -> None:
    fit = fit_additional_context(
        [("agent_prompt", "p" * 60), ("rule:big", "b" * 50), ("rule:small", "s" * 10)],
        limit=120,
    )

    assert fit.text == "p" * 60 + "\n\n" + "s" * 10 + "\n\nomitted contributors=[rule:big]"
    assert fit.omitted == [("rule:big", "b" * 50)]


def test_part_larger_than_the_limit_drops_before_protected_ones() -> None:
    fit = fit_additional_context(
        [("agent_prompt", "p" * 60), ("rule:huge", "h" * 200), ("rule:small", "s" * 10)],
        limit=120,
    )

    assert fit.text == "p" * 60 + "\n\n" + "s" * 10 + "\n\nomitted contributors=[rule:huge]"
    assert fit.omitted == [("rule:huge", "h" * 200)]


def test_part_with_no_room_for_the_breadcrumb_drops_first() -> None:
    fit = fit_additional_context(
        [("rule:x", "a" * 9940), ("rule:y", "b" * 500)], limit=9950, protected={"rule:x"}
    )

    assert fit.text == "b" * 500 + "\n\nomitted contributors=[rule:x]"
    assert fit.omitted == [("rule:x", "a" * 9940)]


def test_fitting_text_reports_nothing_omitted() -> None:
    fit = fit_additional_context([("a", "x"), ("b", "y")], limit=80)

    assert fit.text == "x\n\ny"
    assert fit.omitted == []
