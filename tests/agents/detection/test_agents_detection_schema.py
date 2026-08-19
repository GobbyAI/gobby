"""Tests for detection manifest schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.agents.detection.schema import CURRENT_ENGINE_VERSION, load_manifest

pytestmark = pytest.mark.unit


VALID_MANIFEST = """
id = "claude"
version = "1.2"
engine = 1

[[rules]]
id = "approval_prompt"
state = "blocked"
reason = "approval"
priority = 900
region = "bottom_non_empty_lines(8)"
contains = ["do you want to proceed?"]
line_regex = ['^\\s*❯?\\s*1\\.\\s*yes\\b']
not = [{ contains = ["esc to interrupt"] }]
"""


def test_manifest_schema_validates_shape() -> None:
    manifest = load_manifest(VALID_MANIFEST)

    assert manifest.id == "claude"
    assert manifest.version == "1.2"
    assert manifest.version_key == (1, 2)
    assert manifest.engine == CURRENT_ENGINE_VERSION
    assert manifest.rules[0].not_[0].contains == ("esc to interrupt",)


@pytest.mark.parametrize(
    "content",
    [
        VALID_MANIFEST.replace("engine = 1", f"engine = {CURRENT_ENGINE_VERSION + 1}"),
        VALID_MANIFEST.replace('version = "1.2"', 'version = "latest"'),
        VALID_MANIFEST.replace('region = "bottom_non_empty_lines(8)"', 'region = "viewport"'),
        VALID_MANIFEST.replace('reason = "approval"\n', ""),
        "\n".join(
            line
            for line in VALID_MANIFEST.splitlines()
            if not line.startswith(("contains =", "line_regex ="))
        ),
    ],
)
def test_manifest_schema_rejects_invalid_engine_version_and_rule_shapes(content: str) -> None:
    with pytest.raises(ValidationError):
        load_manifest(content)
