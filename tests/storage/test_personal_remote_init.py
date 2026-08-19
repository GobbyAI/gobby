"""Remote and claimless personal-project init leaves no node-local tree."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.paths import get_gobby_home
from gobby.storage.projects import (
    PERSONAL_PROJECT_ID,
    ensure_personal_project,
    ensure_personal_project_identity,
)

pytestmark = pytest.mark.unit


def _write_remote_bootstrap() -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


@pytest.fixture(autouse=True)
def _restore_bootstrap() -> Iterator[None]:
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    previous = bootstrap.read_bytes() if bootstrap.exists() else None
    yield
    if previous is None:
        bootstrap.unlink(missing_ok=True)
    else:
        bootstrap.write_bytes(previous)
        bootstrap.chmod(0o600)


def test_remote_ensure_upserts_sentinel_without_personal_tree(
    project_manager: object,
    tmp_path: Path,
) -> None:
    _write_remote_bootstrap()
    db = project_manager.db  # type: ignore[attr-defined]

    project = ensure_personal_project(db)

    assert project.id == PERSONAL_PROJECT_ID
    assert project.name == "_personal"
    assert not (tmp_path / "_personal").exists()
    with pytest.raises(Exception, match="held singleton|remote|files_home"):
        ensure_personal_project_identity()


def test_runtime_hub_open_skips_identity_on_remote(tmp_path: Path) -> None:
    _write_remote_bootstrap()
    db = MagicMock()
    with (
        patch("gobby.storage.hub.runtime.load_bootstrap") as load,
        patch("gobby.storage.hub.runtime.admitted_database_url", return_value="postgresql://x"),
        patch("gobby.storage.hub.postgres.PostgresHubDatabase", return_value=db),
        patch("gobby.storage.projects.ensure_personal_project") as ensure,
    ):
        load.return_value.database_url = "postgresql://x"
        load.return_value.postgres_pool = None
        from gobby.storage.hub.runtime import runtime_hub_database

        with runtime_hub_database():
            pass

    ensure.assert_called_once_with(db)


def test_config_open_remote_upserts_sentinel_without_identity(tmp_path: Path) -> None:
    _write_remote_bootstrap()
    db = MagicMock()
    config = MagicMock()
    config.database_url = "postgresql://x"
    config.datastore_mode = "remote"
    config.postgres_pool = None
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=config),
        patch("gobby.storage.hub.postgres.PostgresHubDatabase", return_value=db),
        patch("gobby.storage.projects.ensure_personal_project") as ensure,
        patch("gobby.runner_pid_file.claim_pid_file") as claim,
    ):
        from gobby.cli.utils_config import init_local_storage

        result = init_local_storage()

    assert result is db
    ensure.assert_called_once_with(db)
    claim.assert_not_called()
