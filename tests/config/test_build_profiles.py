"""Red tests for renamed unattended build profiles."""

from __future__ import annotations

import pytest

from gobby.config.build import BuildConfig, resolve_profile

pytestmark = pytest.mark.unit


def test_legacy_yolo_profile_aliases_resolve() -> None:
    cfg = BuildConfig()

    assert "default_unattended" in cfg.profiles
    assert "full-unattended" in cfg.profiles
    assert cfg.profiles["default_unattended"]["yolo"] is False
    assert cfg.profiles["full-unattended"]["yolo"] is True

    assert resolve_profile(cfg, "default_yolo", "#1") == cfg.profiles["default_unattended"]
    assert resolve_profile(cfg, "full-yolo", "#1") == cfg.profiles["full-unattended"]
