from __future__ import annotations

import pytest

from gobby.config.wiki import WikiConfig

pytestmark = pytest.mark.unit


def test_codewiki_scopes_are_stripped_after_validation() -> None:
    config = WikiConfig(codewiki_scopes=[" src/gobby ", "tests "])

    assert config.codewiki_scopes == ["src/gobby", "tests"]


def test_project_codewiki_scopes_are_stripped_after_validation() -> None:
    config = WikiConfig(codewiki_project_scopes_by_name={"gobby": [" crates/gcode "]})

    assert config.codewiki_project_scopes_by_name == {"gobby": ["crates/gcode"]}
