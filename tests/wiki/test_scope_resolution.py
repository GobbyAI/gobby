from __future__ import annotations

import pytest

from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.wiki.scope_resolution import (
    WikiScopeResolutionError,
    resolve_scope_identity,
    resolve_wiki_scope,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_resolve_wiki_scope_empty_project_does_not_fall_back_to_default() -> None:
    with pytest.raises(WikiScopeResolutionError, match="wiki scope cannot be empty"):
        await resolve_wiki_scope(None, project="", default_project_id="fallback-project")


@pytest.mark.asyncio
async def test_personal_sentinel_resolves_without_repo_path() -> None:
    resolved = await resolve_wiki_scope(None, project=PERSONAL_PROJECT_ID)
    assert resolved.project_id == PERSONAL_PROJECT_ID
    assert resolved.topic is None
    alias = await resolve_scope_identity(None, "_personal", require_project_root=False)
    assert alias.project_id == PERSONAL_PROJECT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["personal", "_personal", "wiki"])
async def test_reserved_topic_names_refuse_in_shared_resolver(name: str) -> None:
    with pytest.raises(WikiScopeResolutionError, match="reserved|invalid"):
        await resolve_wiki_scope(None, topic=name)
    with pytest.raises(WikiScopeResolutionError, match="reserved|invalid"):
        await resolve_scope_identity(None, f"topic:{name}")
