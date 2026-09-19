"""Shared fixtures for the terminal-driven session tools."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_enter_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap before the submitting Enter is live-CLI timing, not something to wait on."""
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
