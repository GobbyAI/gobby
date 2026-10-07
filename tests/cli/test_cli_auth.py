from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.auth import auth

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_stores() -> Iterator[MagicMock]:
    with (
        patch("gobby.cli.auth.require_cli_database", return_value=MagicMock()),
        patch("gobby.cli.auth.LocalUserManager") as mock_users,
    ):
        users_inst = mock_users.return_value

        yield users_inst


def test_auth_no_db(mock_stores: MagicMock) -> None:
    runner = CliRunner()
    with patch(
        "gobby.cli.auth.require_cli_database",
        side_effect=RuntimeError("hub missing"),
    ):
        result = runner.invoke(auth, ["credentials"])
    assert result.exit_code == 1
    assert "hub missing" in result.output


def test_auth_requires_canonical_user(mock_stores: MagicMock) -> None:
    users = mock_stores
    users.require_sole_user.side_effect = RuntimeError("No canonical user is installed")
    runner = CliRunner()
    result = runner.invoke(auth, ["credentials"])
    assert result.exit_code == 1
    assert "No canonical user is installed" in result.output


def test_auth_reset_password(mock_stores: MagicMock) -> None:
    users = mock_stores
    users.require_sole_user.return_value = MagicMock(
        id="6c71924f-1e3e-4d16-863a-dfdc3b917fea",
        email="owner@example.com",
    )
    runner = CliRunner()
    with patch("gobby.cli.auth.hash_password", return_value="$argon2id$v=19$params$salt$hash"):
        result = runner.invoke(auth, ["credentials"], input="newpass\nnewpass\n")
    assert result.exit_code == 0
    assert "Resetting web UI password for owner@example.com." in result.output
    assert "Password updated for owner@example.com." in result.output
    users.update_password.assert_called_once_with(
        "6c71924f-1e3e-4d16-863a-dfdc3b917fea",
        "$argon2id$v=19$params$salt$hash",
    )


def test_auth_token_command_is_removed() -> None:
    result = CliRunner().invoke(auth, ["token", "--help"])
    assert result.exit_code == 2
    assert "No such command 'token'" in result.output


def test_auth_group_registers_enrollment_commands() -> None:
    assert set(auth.commands) == {"credentials", "login", "key"}
    login_help = CliRunner().invoke(auth, ["login", "--help"])
    assert login_help.exit_code == 0
    for option in ("--hub", "--email", "--fingerprint", "--label", "--insecure"):
        assert option in login_help.output
