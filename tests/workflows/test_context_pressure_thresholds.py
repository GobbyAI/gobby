"""Adaptive context-pressure threshold boundaries."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gobby.workflows.observer_context_usage import detect_context_compact_guidance

pytestmark = pytest.mark.unit


@dataclass
class _Session:
    context_usage_ratio: float | None
    context_window: int | None
    context_used_tokens: int | None = None


class _SessionManager:
    def __init__(self, ratio: float | None, window: int | None) -> None:
        self.session = _Session(ratio, window)

    def get(self, _session_id: str) -> _Session:
        return self.session


@pytest.mark.parametrize(
    ("window", "ratio", "expected_kind"),
    [
        (999_999, 0.399, ""),
        (999_999, 0.40, "soft"),
        (999_999, 0.60, "strong"),
        (1_000_000, 0.299, ""),
        (1_000_000, 0.30, "soft"),
        (1_000_000, 0.40, "strong"),
        (None, 0.40, "soft"),
        (None, 0.60, "strong"),
    ],
)
def test_threshold_boundaries(window: int | None, ratio: float, expected_kind: str) -> None:
    variables = {"parent_turn_seq": 0, "chat_mode": "normal"}

    detect_context_compact_guidance(
        variables,
        "session-1",
        _SessionManager(ratio, window),
    )

    assert variables["context_compact_guidance_kind"] == expected_kind


def test_soft_guidance_is_emitted_once() -> None:
    variables = {"parent_turn_seq": 0, "chat_mode": "normal"}
    manager = _SessionManager(0.50, 999_999)

    detect_context_compact_guidance(variables, "session-1", manager)
    assert variables["context_compact_guidance_kind"] == "soft"

    variables["parent_turn_seq"] = 1
    detect_context_compact_guidance(variables, "session-1", manager)
    assert variables["context_compact_guidance_message"] == ""


def test_plan_mode_returns_before_turn_accounting() -> None:
    variables = {"parent_turn_seq": 8, "chat_mode": "plan", "turns_since_compact": 4}

    detect_context_compact_guidance(
        variables,
        "session-1",
        _SessionManager(0.90, 1_000_000),
    )

    assert variables["context_compact_guidance_message"] == ""
    assert variables["turns_since_compact"] == 4
