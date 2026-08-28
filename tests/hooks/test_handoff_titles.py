"""Pin that Codex BEFORE_AGENT no longer seeds titles from plan-handoff prompts.

#21140 removed `gobby.hooks.handoff_titles` and the BeforeAgent `update_title`
seed. Keep this module so the historical two-file pytest command still collects.
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.unit


def test_codex_handoff_title_helper_module_is_removed() -> None:
    assert importlib.util.find_spec("gobby.hooks.handoff_titles") is None
