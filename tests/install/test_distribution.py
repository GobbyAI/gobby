"""Tests for distribution-specific install behavior."""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import MagicMock, patch

import pytest

from gobby.install.distribution import (
    HomebrewDistributionError,
    is_homebrew_distribution,
    verify_homebrew_managed_bins,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


def test_homebrew_distribution_detects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_DISTRIBUTION", "homebrew")
    assert is_homebrew_distribution() is True

    monkeypatch.setenv("GOBBY_DISTRIBUTION", "pip")
    assert is_homebrew_distribution() is False


def test_homebrew_helper_detection_fails_with_brew_guidance_when_missing() -> None:
    with patch("gobby.install.distribution.shutil.which", return_value=None):
        with pytest.raises(HomebrewDistributionError) as exc_info:
            verify_homebrew_managed_bins()

    message = str(exc_info.value)
    assert "Homebrew-managed Gobby requires Brew-installed helper binaries on PATH." in message
    assert "gcode >= 0.8.0 required; gcode was not found on PATH." in message
    assert "brew install GobbyAI/tap/gobby-code" in message
    assert "brew upgrade GobbyAI/tap/gobby-code" in message
    assert "brew install GobbyAI/tap/gobby-local" in message


def test_homebrew_helper_detection_fails_with_brew_guidance_when_stale() -> None:
    def fake_which(name: str) -> str:
        return f"/opt/homebrew/bin/{name}"

    def fake_run(args: Sequence[str], **_kwargs: object) -> MagicMock:
        binary = str(args[0]).rsplit("/", maxsplit=1)[-1]
        version = "0.1.0" if binary == "gcode" else MANAGED_BIN_VERSION_PINS[binary]
        return MagicMock(returncode=0, stdout=f"{binary} {version}\n", stderr="")

    with (
        patch("gobby.install.distribution.shutil.which", side_effect=fake_which),
        patch("gobby.install.distribution.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(HomebrewDistributionError) as exc_info:
            verify_homebrew_managed_bins()

    message = str(exc_info.value)
    assert "gcode >= 0.8.0 required; gcode 0.1.0 at /opt/homebrew/bin/gcode is too old." in message
    assert "brew install GobbyAI/tap/gobby-code" in message
    assert "brew upgrade GobbyAI/tap/gobby-code" in message


def test_homebrew_helper_detection_accepts_pinned_versions() -> None:
    def fake_which(name: str) -> str:
        return f"/opt/homebrew/bin/{name}"

    def fake_run(args: Sequence[str], **_kwargs: object) -> MagicMock:
        binary = str(args[0]).rsplit("/", maxsplit=1)[-1]
        return MagicMock(
            returncode=0,
            stdout=f"{binary} {MANAGED_BIN_VERSION_PINS[binary]}\n",
            stderr="",
        )

    with (
        patch("gobby.install.distribution.shutil.which", side_effect=fake_which),
        patch("gobby.install.distribution.subprocess.run", side_effect=fake_run),
    ):
        statuses = verify_homebrew_managed_bins()

    assert [status.name for status in statuses] == ["gcode", "gsqz", "ghook", "gloc"]
    assert all(status.ok for status in statuses)
