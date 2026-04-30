"""Phase 2 contract tests for the active developer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_developer_yaml_exists_at_active_root() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/developer.yaml"
    )

    assert path.exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["name"] == "developer"


def test_deprecated_developer_tombstone_left_in_place() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/deprecated/developer.yaml"
    )

    assert path.exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["name"] == "developer"
