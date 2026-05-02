"""Expansion service facade cleanup contracts."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_facade_does_not_export_skipped_stages() -> None:
    from gobby.tasks import expansion_service

    assert not hasattr(expansion_service, "_skipped_stages")
    assert "_skipped_stages" not in getattr(expansion_service, "__all__", ())
