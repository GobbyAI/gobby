"""Telegram agent labels name a seat by its definition, else by its provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.communications.agent_labels import agent_label, telegram_agent_label
from gobby.storage.session_models import Session


def _session(source: str, ref: str = "gobby#15976") -> Session:
    return cast(Session, SimpleNamespace(source=source, ref=ref))


@pytest.mark.parametrize(
    ("source", "seat_label", "expected"),
    [
        ("agy", None, "Antigravity"),
        ("claude_code", None, "Claude Code"),
        ("some_source", None, "some_source"),
        ("", None, "Unknown"),
        ("agy", "Lane Bravo", "Lane Bravo"),
    ],
)
def test_agent_label_names_the_seat_then_its_provider(
    source: str, seat_label: str | None, expected: str
) -> None:
    assert agent_label(_session(source), seat_label) == expected


@pytest.mark.parametrize(
    ("seat_label", "expected"),
    [
        ("Alpha Seat", "Alpha Seat (gobby#15976)"),
        ("Lane\nBravo", "Lane Bravo (gobby#15976)"),
        ("A" * 200, "A" * 80 + " (gobby#15976)"),
        (None, "Codex (gobby#15976)"),
    ],
)
def test_telegram_label_keeps_origin_reference(seat_label: str | None, expected: str) -> None:
    assert telegram_agent_label(_session("codex"), seat_label) == expected


def test_telegram_label_uses_fixed_roles_for_responder_and_system_sources() -> None:
    assert telegram_agent_label(_session("comms"), "Alpha Seat") == "Responder (gobby#15976)"
    assert telegram_agent_label(_session("system"), "Alpha Seat") == "System (gobby#15976)"


def test_sessionless_telegram_label_is_stable() -> None:
    assert telegram_agent_label(None) == "System (gobby)"
