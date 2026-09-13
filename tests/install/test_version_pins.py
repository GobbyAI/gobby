from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gobby.install import version_pins
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("bin_name", "crate_dir"),
    [
        ("gdaemon", "gdaemon"),
        ("gterm", "gterminal"),
        ("gclient", "gclient"),
    ],
)
def test_managed_bin_pins_match_crate_versions(bin_name: str, crate_dir: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "crates" / crate_dir / "Cargo.toml"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    assert MANAGED_BIN_VERSION_PINS[bin_name] == manifest["package"]["version"]


def test_published_state_is_explicit_for_managed_binaries() -> None:
    unpublished = version_pins.UNPUBLISHED_MANAGED_BINS
    is_published = version_pins.is_published

    assert unpublished == frozenset({"gterm", "gclient"})
    assert is_published("gterm") is False
    assert is_published("gclient") is False
    assert is_published("gdaemon") is True
