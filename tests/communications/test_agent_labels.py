"""Telegram agent labels fall back to the shared provider display names."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.communications.agent_labels import agent_label, telegram_agent_label
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


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Assistant", "Assistant (gobby#15976)"),
        ("gobby#15976: Reporter", "Reporter (gobby#15976)"),
        ("Lane\nDeveloper", "Lane Developer (gobby#15976)"),
        ("A" * 200, "A" * 80 + " (gobby#15976)"),
    ],
)
def test_telegram_label_keeps_origin_reference(title: str, expected: str) -> None:
    session = cast(Session, SimpleNamespace(source="codex", title=title, ref="gobby#15976"))
    assert telegram_agent_label(session) == expected


def test_sessionless_telegram_label_is_stable() -> None:
    assert telegram_agent_label(None) == "System (gobby)"
