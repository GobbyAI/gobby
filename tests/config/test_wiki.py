from __future__ import annotations

import pytest

from gobby.config.wiki import DEFAULT_WIKI_IGNORE_GLOBS, WikiConfig

pytestmark = pytest.mark.unit


def test_default_ignore_globs_cover_librarian_and_upkeep_artifacts() -> None:
    config = WikiConfig()

    assert config.ignore_globs == list(DEFAULT_WIKI_IGNORE_GLOBS)
    assert {
        "outputs/**",
        "meta/health/**",
        "meta/librarian/**",
        "meta/upkeep/**",
        "_meta/**",
        "raw/**",
        "inbox/**",
        "_gwiki/**",
    } <= set(config.ignore_globs)


def test_codewiki_scopes_are_stripped_after_validation() -> None:
    config = WikiConfig(codewiki_scopes=[" src/gobby ", "tests "])

    assert config.codewiki_scopes == ["src/gobby", "tests"]


def test_project_codewiki_scopes_are_stripped_after_validation() -> None:
    config = WikiConfig(codewiki_project_scopes_by_name={"gobby": [" crates/gcode "]})

    assert config.codewiki_project_scopes_by_name == {"gobby": ["crates/gcode"]}
