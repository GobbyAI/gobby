from __future__ import annotations

import pytest

from gobby.hooks.code_navigation import count_option_line_count

pytestmark = pytest.mark.unit


def test_count_option_line_count_accepts_compact_options_after_command() -> None:
    assert count_option_line_count(["head", "-n5", "src/gobby/app.py"]) == 5
    assert count_option_line_count(["tail", "-5", "src/gobby/app.py"]) == 5
