"""Red tests for build-agent rule audience scoping."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_build_rules_autonomous_only() -> None:
    rule_dir = Path("src/gobby/install/shared/rules/build")
    rule_files = [path for path in rule_dir.glob("*.yaml") if path.is_file()]

    assert rule_files, "build rule YAML files must be bundled"
    for path in rule_files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["audience"] == "autonomous", path
