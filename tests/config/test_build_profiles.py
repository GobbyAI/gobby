"""Red tests for renamed unattended build profiles."""

from __future__ import annotations

import pytest

from gobby.config.build import BuildConfig, resolve_profile

pytestmark = pytest.mark.unit


def test_legacy_yolo_profile_aliases_resolve() -> None:
    cfg = BuildConfig()

    assert "default_unattended" in cfg.profiles
    assert "full-yolo" in cfg.profiles
    assert cfg.profiles["default_unattended"]["yolo"] is False
    assert cfg.profiles["full-yolo"]["yolo"] is True

    assert resolve_profile(cfg, "default_yolo", "#1") == cfg.profiles["default_unattended"]
    assert resolve_profile(cfg, "full-unattended", "#1") == cfg.profiles["full-yolo"]
    assert resolve_profile(cfg, "full-yolo", "#1") == cfg.profiles["full-yolo"]


def test_builtin_profiles_only_reference_surviving_stages() -> None:
    cfg = BuildConfig()
    dropped = {"adversarial_review", "expansion_qa", "code_review_qa"}

    for profile in cfg.profiles.values():
        assert dropped.isdisjoint(profile["skip_stages"])
