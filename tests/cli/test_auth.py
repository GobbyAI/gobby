from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.auth import auth


@pytest.fixture
def mock_stores():
    with (
        patch("gobby.cli.auth.get_gobby_home") as mock_home,
        patch("gobby.cli.auth.LocalDatabase"),
        patch("gobby.cli.auth.ConfigStore") as mock_config,
        patch("gobby.cli.auth.SecretStore") as mock_secret,
    ):
        mock_home_path = MagicMock()
        mock_final_path = MagicMock()
        mock_final_path.exists.return_value = True
        mock_home_path.__truediv__.return_value = mock_final_path
        mock_home.return_value = mock_home_path

        config_inst = mock_config.return_value
        secret_inst = mock_secret.return_value

        yield config_inst, secret_inst, mock_final_path


def test_auth_no_db(mock_stores):
    config, secret, path = mock_stores
    path.exists.return_value = False
    runner = CliRunner()
    result = runner.invoke(auth, [])
    assert result.exit_code == 1
    assert "Error: Gobby database not found" in result.output


def test_auth_remove_not_configured(mock_stores):
    config, secret, path = mock_stores
    config.get.return_value = None
    runner = CliRunner()
    result = runner.invoke(auth, ["--remove"])
    assert result.exit_code == 0
    assert "No auth configured. Nothing to remove." in result.output


def test_auth_remove_configured(mock_stores):
    config, secret, path = mock_stores
    config.get.return_value = "admin"
    runner = CliRunner()
    result = runner.invoke(auth, ["--remove"])
    assert result.exit_code == 0
    assert "Auth removed for user 'admin'." in result.output
    config.delete.assert_called_with("auth.username")
    config.clear_secret.assert_called_with("auth.password", secret)


def test_auth_setup_new(mock_stores):
    config, secret, path = mock_stores
    config.get.return_value = None
    runner = CliRunner()
    # Provide username, password, confirm password
    result = runner.invoke(auth, [], input="admin\nmypass\nmypass\n")
    assert result.exit_code == 0
    assert "Auth enabled for user 'admin'." in result.output
    config.set.assert_called_with("auth.username", "admin", source="user")
    config.set_secret.assert_called_with("auth.password", "mypass", secret, source="user")


def test_auth_reset_password(mock_stores):
    config, secret, path = mock_stores
    config.get.return_value = "admin"
    runner = CliRunner()
    # Provide password, confirm password
    result = runner.invoke(auth, [], input="newpass\nnewpass\n")
    assert result.exit_code == 0
    assert "Password updated for user 'admin'." in result.output
    config.set_secret.assert_called_with("auth.password", "newpass", secret, source="user")
