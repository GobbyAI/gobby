"""Public contract tests for the summary-actions facade."""

import pytest

from gobby.sessions import summary_formatting, summary_generation, tmux_window_naming
from gobby.workflows import summary_actions

pytestmark = pytest.mark.unit


def test_facade_exports_owner_implementations() -> None:
    assert summary_actions.__all__ == [
        "format_unresolved_errors",
        "format_turns_for_llm",
        "generate_summary",
        "schedule_tmux_window_rename",
        "enforce_window_name_if_unmanaged",
    ]
    assert {name for name in vars(summary_actions) if not name.startswith("_")} == set(
        summary_actions.__all__
    )
    assert summary_actions.format_unresolved_errors is summary_formatting.format_unresolved_errors
    assert summary_actions.format_turns_for_llm is summary_formatting.format_turns_for_llm
    assert summary_actions.generate_summary is summary_generation.generate_summary
    assert (
        summary_actions.schedule_tmux_window_rename
        is tmux_window_naming.schedule_tmux_window_rename
    )
    assert (
        summary_actions.enforce_window_name_if_unmanaged
        is tmux_window_naming.enforce_window_name_if_unmanaged
    )
