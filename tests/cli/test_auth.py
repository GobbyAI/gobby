from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.auth import auth
from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    hash_token,
    rotate_local_api_token,
)


@pytest.fixture
def mock_stores():
    @contextmanager
    def fake_hub():
        yield MagicMock()

    with (
        patch("gobby.cli.auth.runtime_hub_database", return_value=fake_hub()),
        patch("gobby.cli.auth.ConfigStore") as mock_config,
        patch("gobby.cli.auth.SecretStore") as mock_secret,
    ):
        config_inst = mock_config.return_value
        secret_inst = mock_secret.return_value

        yield config_inst, secret_inst


def test_auth_no_db(mock_stores):
    config, secret = mock_stores
    runner = CliRunner()
    with patch("gobby.cli.auth.runtime_hub_database", side_effect=RuntimeError("hub missing")):
        result = runner.invoke(auth, ["credentials"])
    assert result.exit_code == 1
    assert "hub missing" in result.output


def test_auth_remove_not_configured(mock_stores):
    config, secret = mock_stores
    config.get.return_value = None
    runner = CliRunner()
    result = runner.invoke(auth, ["credentials", "--remove"])
    assert result.exit_code == 0
    assert "No auth configured. Nothing to remove." in result.output


def test_auth_remove_configured(mock_stores):
    config, secret = mock_stores
    config.get.return_value = "admin"
    runner = CliRunner()
    result = runner.invoke(auth, ["credentials", "--remove"])
    assert result.exit_code == 0
    assert "Auth removed for user 'admin'." in result.output
    config.delete.assert_any_call("auth.username")
    assert config.delete.call_count == 2
    config.delete.assert_any_call("auth.password_hash")
    config.clear_secret.assert_called_with("auth.password", secret)


def test_auth_setup_new(mock_stores):
    config, secret = mock_stores
    config.get.return_value = None
    runner = CliRunner()
    # Provide username, password, confirm password
    with patch("gobby.cli.auth.hash_password", return_value="scrypt$16384$8$1$salt$hash"):
        result = runner.invoke(auth, ["credentials"], input="admin\nmypass\nmypass\n")
    assert result.exit_code == 0
    assert "Auth enabled for user 'admin'." in result.output
    config.set.assert_any_call("auth.username", "admin", source="user")
    assert config.set.call_count == 2
    config.set.assert_any_call(
        "auth.password_hash",
        "scrypt$16384$8$1$salt$hash",
        source="user",
    )
    config.set_secret.assert_not_called()
    config.clear_secret.assert_called_once_with("auth.password", secret)


def test_auth_reset_password(mock_stores):
    config, secret = mock_stores
    config.get.return_value = "admin"
    runner = CliRunner()
    # Provide password, confirm password
    with patch("gobby.cli.auth.hash_password", return_value="scrypt$16384$8$1$salt$hash"):
        result = runner.invoke(auth, ["credentials"], input="newpass\nnewpass\n")
    assert result.exit_code == 0
    assert "Password updated for user 'admin'." in result.output
    config.set.assert_called_once_with(
        "auth.password_hash",
        "scrypt$16384$8$1$salt$hash",
        source="user",
    )
    config.set_secret.assert_not_called()
    config.clear_secret.assert_called_once_with("auth.password", secret)


def test_auth_group_has_credentials_and_token_commands() -> None:
    assert set(auth.commands) == {"credentials", "token"}
    help_result = CliRunner().invoke(auth, ["token", "--help"])
    assert help_result.exit_code == 0
    assert "gobby auth token --rotate" in help_result.output


def test_auth_token_status_and_show(
    mock_stores: tuple[MagicMock, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _secret = mock_stores
    gobby_home = tmp_path / "gobby-home"
    token_path = gobby_home / "local_cli_token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("local-token")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    config.get.return_value = hash_token("local-token")

    result = CliRunner().invoke(auth, ["token", "--show"])

    assert result.exit_code == 0
    assert str(token_path) in result.output
    assert "File: exists" in result.output
    assert f"Stored hash: sha256:{hash_token('local-token')[:8]}…" in result.output
    assert "File and DB agree: yes" in result.output
    assert "Token: local-token" in result.output


def test_auth_token_rotate(
    mock_stores: tuple[MagicMock, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _secret = mock_stores
    gobby_home = tmp_path / "gobby-home"
    token_path = gobby_home / "local_cli_token"
    token_path.parent.mkdir(parents=True)
    old_token = "old-token"
    token_path.write_text(old_token)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    stored = {LOCAL_API_TOKEN_HASH_KEY: hash_token(old_token)}
    config.get.side_effect = stored.get

    def set_config(key: str, value: str, source: str) -> None:
        assert source == "system"
        stored[key] = value

    config.set.side_effect = set_config
    with patch(
        "gobby.cli.auth.rotate_local_api_token",
        wraps=rotate_local_api_token,
    ) as mock_rotate:
        result = CliRunner().invoke(auth, ["token", "--rotate"])

    assert result.exit_code == 0
    mock_rotate.assert_called_once_with(config)
    new_token = token_path.read_text().strip()
    assert new_token != old_token
    assert stored[LOCAL_API_TOKEN_HASH_KEY] == hash_token(new_token)
    assert stored[LOCAL_API_TOKEN_HASH_KEY] != hash_token(old_token)
    assert "Local API token rotated." in result.output
    assert "within ~5 seconds" in result.output
    assert "Recopy" in result.output
    assert "File and DB agree: yes" in result.output
