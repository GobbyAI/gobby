"""Tests for feature-level prompt rendering."""

from __future__ import annotations

import pytest

from gobby.llm.prompt_rendering import render_summary_prompt

pytestmark = pytest.mark.unit


def test_render_summary_prompt_with_jinja2() -> None:
    context = {
        "transcript_summary": "User asked about Python",
        "last_messages": [{"role": "user", "content": "hi"}],
        "git_status": "clean",
        "file_changes": "none",
    }

    result = render_summary_prompt(
        "Summary: {{ transcript_summary }}\nMessages: {{ last_messages }}",
        context,
    )

    assert "User asked about Python" in result
    assert '"role": "user"' in result


def test_render_summary_prompt_with_format_template() -> None:
    result = render_summary_prompt(
        "Summary: {transcript_summary}\nGit: {git_status}",
        {"transcript_summary": "Done", "git_status": "clean"},
    )

    assert result == "Summary: Done\nGit: clean"


def test_render_summary_prompt_raises_without_template() -> None:
    with pytest.raises(ValueError, match="prompt_template is required"):
        render_summary_prompt("", {})


def test_render_summary_prompt_includes_extra_context_keys() -> None:
    result = render_summary_prompt("Custom: {{ custom_key }}", {"custom_key": "custom_value"})

    assert result == "Custom: custom_value"
