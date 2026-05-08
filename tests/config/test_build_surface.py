"""Tests for the profile-free build configuration surface."""

from __future__ import annotations

import pytest

from gobby.config.build import BuildConfig

pytestmark = pytest.mark.unit


def test_build_config_has_no_profile_or_yolo_knobs() -> None:
    cfg = BuildConfig()

    assert not hasattr(cfg, "profiles")
    assert not hasattr(cfg, "default_yolo")
