"""Regression tests for bundled pipeline skill-loading boundaries."""

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

PIPELINES_DIR = Path("src/gobby/install/shared/workflows/pipelines")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def test_bundled_pipelines_do_not_load_skills() -> None:
    pipeline_paths = sorted(PIPELINES_DIR.glob("*.yaml"))
    assert pipeline_paths

    for path in pipeline_paths:
        text = path.read_text()
        assert "get_skill" not in text, path
        assert "load_skill" not in text, path
        assert "injected_skills" not in text, path

        data = yaml.safe_load(text)
        for node in _walk(data):
            assert not (
                node.get("server") == "gobby-skills"
                and node.get("tool") == "get_skill"
                and node.get("inject_result") is True
            ), path
