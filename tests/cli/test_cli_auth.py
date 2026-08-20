from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.auth import auth
from gobby.storage.auth import hash_token, rotate_local_api_token

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_stores() -> Iterator[tuple[MagicMock, MagicMock]]:
    with (
        patch("gobby.cli.auth.require_cli_database", return_value=MagicMock()),
        patch("gobby.cli.auth.AuthStore") as mock_auth_store,
        patch("gobby.cli.auth.LocalUserManager") as mock_users,
    ):
        config_inst = mock_auth_store.return_value
        users_inst = mock_users.return_value

        yield config_inst, users_inst


def test_auth_no_db(mock_stores: tuple[MagicMock, MagicMock]) -> None:
    runner = CliRunner()
    with patch(
        "gobby.cli.auth.require_cli_database",
        side_effect=RuntimeError("hub missing"),
    ):
        result = runner.invoke(auth, ["credentials"])
    assert result.exit_code == 1
    assert "hub missing" in result.output


def test_auth_requires_canonical_user(mock_stores: tuple[MagicMock, MagicMock]) -> None:
    _config, users = mock_stores
    users.require_sole_user.side_effect = RuntimeError("No canonical user is installed")
    runner = CliRunner()
    result = runner.invoke(auth, ["credentials"])
    assert result.exit_code == 1
    assert "No canonical user is installed" in result.output


def test_auth_reset_password(mock_stores: tuple[MagicMock, MagicMock]) -> None:
    _config, users = mock_stores
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
    config, _users = mock_stores
    gobby_home = tmp_path / "gobby-home"
    token_path = gobby_home / "local_cli_token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("local-token")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    config.get_local_api_token_hash.return_value = hash_token("local-token")

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
    config, _users = mock_stores
    gobby_home = tmp_path / "gobby-home"
    token_path = gobby_home / "local_cli_token"
    token_path.parent.mkdir(parents=True)
    old_token = "old-token"
    token_path.write_text(old_token)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    stored_hash = hash_token(old_token)
    config.get_local_api_token_hash.side_effect = lambda: stored_hash

    def set_token_hash(value: str) -> None:
        nonlocal stored_hash
        stored_hash = value

    config.set_local_api_token_hash.side_effect = set_token_hash
    with patch(
        "gobby.cli.auth.rotate_local_api_token",
        wraps=rotate_local_api_token,
    ) as mock_rotate:
        result = CliRunner().invoke(auth, ["token", "--rotate"])

    assert result.exit_code == 0
    mock_rotate.assert_called_once_with(config)
    new_token = token_path.read_text().strip()
    assert new_token != old_token
    assert stored_hash == hash_token(new_token)
    assert stored_hash != hash_token(old_token)
    assert "Local API token rotated." in result.output
    assert "within ~5 seconds" in result.output
    assert "Recopy" in result.output
    assert "File and DB agree: yes" in result.output
