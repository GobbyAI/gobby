from __future__ import annotations

import pytest

from gobby.wiki.scope_resolution import WikiScopeResolutionError, resolve_wiki_scope

pytestmark = pytest.mark.unit


def test_resolve_wiki_scope_empty_project_does_not_fall_back_to_default() -> None:
    with pytest.raises(WikiScopeResolutionError, match="wiki scope cannot be empty"):
        resolve_wiki_scope(None, project="", default_project_id="fallback-project")
