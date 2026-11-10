"""The workspace dev profile keeps line tables so a panic still names its file and line."""

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_dev_profile_keeps_line_tables_for_panics() -> None:
    manifest = tomllib.loads((REPOSITORY_ROOT / "Cargo.toml").read_text(encoding="utf-8"))

    assert manifest["profile"]["dev"]["debug"] == "line-tables-only"
