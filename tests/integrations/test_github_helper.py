from __future__ import annotations

import pytest

from gobby.integrations.github_helper import _github_page_limit


def test_github_page_limit_accepts_api_range() -> None:
    assert _github_page_limit(1) == 1
    assert _github_page_limit(100) == 100


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_github_page_limit_rejects_silent_caps(limit: int) -> None:
    with pytest.raises(ValueError):
        _github_page_limit(limit)
