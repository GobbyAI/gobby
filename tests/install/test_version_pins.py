from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from gobby.install import version_pins
from gobby.install.bin_release_identity import RELEASE_CRATES
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _manifest(bin_name: str) -> dict[str, Any]:
    manifest_path = REPO_ROOT / "crates" / RELEASE_CRATES[bin_name] / "Cargo.toml"
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


@pytest.mark.parametrize("bin_name", sorted(MANAGED_BIN_VERSION_PINS))
def test_managed_bin_pins_match_crate_versions(bin_name: str) -> None:
    assert MANAGED_BIN_VERSION_PINS[bin_name] == _manifest(bin_name)["package"]["version"]


@pytest.mark.parametrize("bin_name", sorted(MANAGED_BIN_VERSION_PINS))
def test_managed_bin_pins_match_cargo_lock_versions(bin_name: str) -> None:
    """Cargo.lock's workspace entry must carry the same version as the pin and manifest."""
    crate_name = _manifest(bin_name)["package"]["name"]
    lock = tomllib.loads((REPO_ROOT / "Cargo.lock").read_text(encoding="utf-8"))
    workspace_entries = [
        package
        for package in lock["package"]
        if package["name"] == crate_name and "source" not in package
    ]

    assert len(workspace_entries) == 1, f"{crate_name} workspace entry missing from Cargo.lock"
    assert workspace_entries[0]["version"] == MANAGED_BIN_VERSION_PINS[bin_name]


@pytest.mark.parametrize(
    ("bin_name", "minimum"),
    [
        # gmcp: call_tool.rs changed in #24893 under installed 0.1.13.
        # gmcp: gcore changed in #25195 under installed 0.1.16.
        # gmcp: release-token contract removed in #25233 under installed 0.1.17.
        ("gmcp", "0.1.18"),
        # gclient: pane_chrome changed in #25128/#24901 and gcore in #24451 under installed 0.1.86.
        # gclient: gcore changed in #25195 under installed 0.1.88.
        ("gclient", "0.1.89"),
        # gcode, gdaemon, ghook: gcore changed in #25195 under installed 1.9.79, 0.4.112, 0.9.51.
        # gcode: commit-bound search scope verification changed in #24029 under installed 1.9.80.
        # gcode: admitted-version read binding changed in #23834 under installed 1.9.81.
        ("gcode", "1.9.82"),
        # gcode: history eligibility at the current config changed in #23582 under installed 1.9.105.
        ("gcode", "1.9.106"),
        # gcode: reconcile apply under the maintenance lease changed in #23580 under installed 1.9.106.
        ("gcode", "1.9.107"),
        # gcode: prune help text and dead inventory removal changed in #25799 under installed 1.9.107.
        ("gcode", "1.9.108"),
        # gcode: vector prune on sync, missing-vector repair, explicit-file repair scope, pinned-overlay scroll guard, bounded hook lock wait and timed-out lock paths persisted for the next run changed in #25810 under installed 1.9.108.
        ("gcode", "1.9.109"),
        ("gdaemon", "0.4.113"),
        ("ghook", "0.9.52"),
    ],
)
def test_changed_source_floors_cover_their_patch_bumps(bin_name: str, minimum: str) -> None:
    """Release guidance: a shipped binary whose sources changed gets a patch bump."""
    assert _version_tuple(MANAGED_BIN_VERSION_PINS[bin_name]) >= _version_tuple(minimum)


def test_published_state_is_explicit_for_managed_binaries() -> None:
    unpublished = version_pins.UNPUBLISHED_MANAGED_BINS
    is_published = version_pins.is_published

    assert unpublished == frozenset({"gterm", "gclient", "gmcp"})
    assert is_published("gterm") is False
    assert is_published("gclient") is False
    assert is_published("gmcp") is False
    assert is_published("gdaemon") is True
