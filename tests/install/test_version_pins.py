from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


def test_gdaemon_pin_matches_crate_version() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "crates" / "gdaemon" / "Cargo.toml"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    assert MANAGED_BIN_VERSION_PINS["gdaemon"] == manifest["package"]["version"]
