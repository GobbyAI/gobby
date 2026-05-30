"""Contract tests for retired bundled agent definitions."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

AGENTS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents"
RETIRED_AGENTS = ("developer", "pipeline-worker")


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles(name: str) -> None:
    active_path = AGENTS_DIR / f"{name}.yaml"
    deprecated_path = AGENTS_DIR / "deprecated" / f"{name}.yaml"

    assert not active_path.exists(), f"retired agent remains active: {active_path}"
    assert not deprecated_path.exists(), f"retired agent deprecated YAML remains: {deprecated_path}"
