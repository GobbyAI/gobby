"""Telegram agent labels fall back to the shared provider display names."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.communications.agent_labels import agent_label
from gobby.storage.session_models import Session


def _session(source: str, title: str | None) -> Session:
    return cast(Session, SimpleNamespace(source=source, title=title))


@pytest.mark.parametrize(
    ("source", "title", "expected"),
    [
        ("agy", None, "Antigravity"),
        ("agy", "gobby#42", "Antigravity"),
        ("claude_code", None, "Claude Code"),
        ("some_source", None, "Some Source"),
        ("agy", "Lane Developer", "Lane Developer"),
    ],
)
def test_agent_label_names_the_provider_for_untitled_sessions(
    source: str, title: str | None, expected: str
) -> None:
    assert agent_label(_session(source, title)) == expected
