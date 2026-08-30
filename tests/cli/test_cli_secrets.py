"""Tests for secrets CLI command wiring."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
)

pytestmark = pytest.mark.unit

secrets_module = importlib.import_module("gobby.cli.secrets")


def test_migrate_command_is_removed() -> None:
    assert "migrate" not in secrets_module.secrets.commands


class _StoreContext:
    def __init__(self, store: MagicMock) -> None:
        self.store = store

    def __enter__(self) -> MagicMock:
        return self.store

    def __exit__(self, *args: object) -> None:
        return None


def test_rekey_passphrase_uses_new_env(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.current_kek_posture.side_effect = [POSTURE_KEY_FILE, POSTURE_SCRYPT_PASSPHRASE]
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "current horse")
    monkeypatch.setenv(secrets_module.NEW_SECRET_KEK_PASSPHRASE_ENV, "replacement horse")

    result = CliRunner().invoke(
        secrets_module.secrets,
        ["rekey", "--posture", "passphrase"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    store.set_kek_posture.assert_called_once_with(
        POSTURE_SCRYPT_PASSPHRASE,
        passphrase="replacement horse",
    )
    assert "key-file -> scrypt-passphrase" in result.output


def test_rekey_key_file_uses_current_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.current_kek_posture.side_effect = [POSTURE_SCRYPT_PASSPHRASE, POSTURE_KEY_FILE]
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "current horse")

    result = CliRunner().invoke(
        secrets_module.secrets,
        ["rekey", "--posture", "key-file"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert store.kek_passphrase == "current horse"
    store.set_kek_posture.assert_called_once_with(POSTURE_KEY_FILE, passphrase=None)
    assert "scrypt-passphrase -> key-file" in result.output


def _project(name: str, project_id: str) -> MagicMock:
    project = MagicMock()
    project.id = project_id
    project.name = name
    return project


def test_set_secret_defaults_to_current_project_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.db = MagicMock()
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    info = MagicMock()
    info.name = "api_key"
    config_store = MagicMock()
    config_store.set_named_secret.return_value = info
    project = _project("game-goblins", "proj-uuid")

    with (
        patch("gobby.storage.config_store.ConfigStore", return_value=config_store),
        patch(
            "gobby.cli.installers.shared.registered_project_id",
            return_value="proj-uuid",
        ) as registered,
        patch("gobby.storage.projects.LocalProjectManager") as manager_cls,
    ):
        manager_cls.return_value.get.return_value = project
        manager_cls.return_value.resolve_ref.return_value = project
        result = CliRunner().invoke(
            secrets_module.secrets,
            ["set", "API_KEY", "--stdin"],
            input="super-secret\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    registered.assert_called_once_with(store.db, Path.cwd())
    assert config_store.set_named_secret.call_args.kwargs["project_id"] == "proj-uuid"
    assert "Stored secret 'api_key' (scope: project game-goblins)" in result.output


def test_set_secret_global_flag_writes_global_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.db = MagicMock()
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    info = MagicMock()
    info.name = "api_key"
    config_store = MagicMock()
    config_store.set_named_secret.return_value = info

    with patch("gobby.storage.config_store.ConfigStore", return_value=config_store):
        result = CliRunner().invoke(
            secrets_module.secrets,
            ["set", "API_KEY", "--global", "--stdin"],
            input="super-secret\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert config_store.set_named_secret.call_args.kwargs.get("project_id") in {None, ""}
    assert "Stored secret 'api_key' (scope: global)" in result.output


def test_list_secrets_prints_scope_column(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.db = MagicMock()
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    global_item = MagicMock()
    global_item.name = "global_key"
    global_item.category = "general"
    global_item.description = ""
    global_item.scope = "global"
    project_item = MagicMock()
    project_item.name = "project_key"
    project_item.category = "llm"
    project_item.description = "desc"
    project_item.scope = "project"
    store.list.return_value = [global_item, project_item]

    with patch(
        "gobby.cli.installers.shared.registered_project_id",
        return_value=None,
    ):
        result = CliRunner().invoke(secrets_module.secrets, ["list"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "SCOPE" in result.output
    assert "global" in result.output
    assert "project" in result.output
    store.list.assert_called_once()


def test_get_and_delete_accept_project_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.db = MagicMock()
    store.exists.return_value = True
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    project = _project("game-goblins", "proj-uuid")
    config_store = MagicMock()
    config_store.delete_named_secret.return_value = True

    with (
        patch("gobby.storage.projects.LocalProjectManager") as manager_cls,
        patch("gobby.storage.config_store.ConfigStore", return_value=config_store),
    ):
        manager_cls.return_value.resolve_ref.return_value = project
        manager_cls.return_value.get.return_value = project
        get_result = CliRunner().invoke(
            secrets_module.secrets,
            ["get", "API_KEY", "--project", "proj-uuid"],
            catch_exceptions=False,
        )
        delete_result = CliRunner().invoke(
            secrets_module.secrets,
            ["delete", "API_KEY", "--project", "proj-uuid", "--yes"],
            catch_exceptions=False,
        )

    assert get_result.exit_code == 0
    assert delete_result.exit_code == 0
    exists_kwargs: dict[str, Any] = store.exists.call_args.kwargs
    assert exists_kwargs.get("project_id") == "proj-uuid" or store.exists.call_args.args[-1] == (
        "proj-uuid"
    )
    assert config_store.delete_named_secret.call_args.kwargs.get("project_id") == "proj-uuid"


def test_delete_exact_scope_miss_reports_not_found_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.db = MagicMock()
    store.exists.return_value = True
    store.find_persisted_secret_references.return_value = set()
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    project = _project("game-goblins", "proj-uuid")
    config_store = MagicMock()
    config_store.delete_named_secret.return_value = False

    with (
        patch("gobby.storage.projects.LocalProjectManager") as manager_cls,
        patch("gobby.storage.config_store.ConfigStore", return_value=config_store),
    ):
        manager_cls.return_value.resolve_ref.return_value = project
        result = CliRunner().invoke(
            secrets_module.secrets,
            ["delete", "API_KEY", "--project", "proj-uuid", "--yes"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "not found in project game-goblins scope" in result.output
    assert "Deleted" not in result.output


def test_delete_referenced_secret_is_refused_with_reference_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.db = MagicMock()
    store.exists.return_value = True
    store.find_persisted_secret_references.return_value = {"api_key"}
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    project = _project("game-goblins", "proj-uuid")
    config_store = MagicMock()
    config_store.delete_named_secret.return_value = False

    with (
        patch("gobby.storage.projects.LocalProjectManager") as manager_cls,
        patch("gobby.storage.config_store.ConfigStore", return_value=config_store),
    ):
        manager_cls.return_value.resolve_ref.return_value = project
        result = CliRunner().invoke(
            secrets_module.secrets,
            ["delete", "API_KEY", "--project", "proj-uuid", "--yes"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "still referenced by stored configuration" in result.output
    assert "Deleted" not in result.output
