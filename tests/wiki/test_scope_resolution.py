from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    GLOBAL_PROJECT_ID,
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
)
from gobby.wiki.scope_resolution import (
    WikiScopeResolutionError,
    _resolve_project_root_sync,
    resolve_scope_identity,
    resolve_wiki_scope,
)
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    install_isolated_checkout_project,
    patch_local_machine_id,
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


@pytest.mark.asyncio
async def test_remote_owner_dispatch_runs_before_local_path_existence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.paths import get_gobby_home
    from gobby.wiki.owner_dispatch import should_proxy_owner_scope

    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    bootstrap.write_text(
        "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)

    def boom(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("local path existence must not run first")

    with patch("gobby.paths.require_files_home", side_effect=boom):
        assert should_proxy_owner_scope(project=None, topic="research") is True
        assert should_proxy_owner_scope(project=PERSONAL_PROJECT_ID, topic=None) is True
        assert should_proxy_owner_scope(project="checkout-id", topic=None) is False


def test_wiki_resolve_project_root_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )

    root = _resolve_project_root_sync(temp_db, isolated.project.id)

    assert root == Path(isolated.root_path).resolve()


def test_wiki_resolve_project_root_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="wiki-no-checkout")

    with pytest.raises(CheckoutNotFoundError):
        _resolve_project_root_sync(temp_db, project.id)


@pytest.mark.asyncio
async def test_wiki_skips_require_root_for_checkout_free_sentinels(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.project_checkouts import require_root

    calls: list[str] = []
    real = require_root

    def spy(db: HubDatabase, project_id: str, machine_id: str | None) -> str:
        calls.append(project_id)
        return real(db, project_id, machine_id)

    monkeypatch.setattr("gobby.storage.project_checkouts.require_root", spy)

    resolved = await resolve_scope_identity(temp_db, GLOBAL_PROJECT_ID, require_project_root=False)

    assert calls == []
    assert resolved.project_id == GLOBAL_PROJECT_ID
    assert GLOBAL_PROJECT_ID in CHECKOUT_FREE_PROJECT_IDS
