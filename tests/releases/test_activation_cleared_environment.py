"""A process whose spawn markers are cleared passes only when it can read the operator credential."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gobby.releases import activation
from gobby.releases.activation import agent_activation_refusal
from gobby.utils import local_token

pytestmark = pytest.mark.unit

_MARKERS = ("GOBBY_AGENT_RUN_ID", "GOBBY_MANAGED_EXECUTION_ID", "GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
_OPERATIONS = ["start", "stop", "restart", "cutover"]
# The passwd home as this process sees it, read before any fixture redirects it.
_REAL_ACCOUNT_HOME = local_token.account_home() or Path("/nonexistent")


@pytest.fixture
def cleared_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_cleared_markers_without_operator_credential_are_refused(
    monkeypatch: pytest.MonkeyPatch, cleared_markers: None, operation: str
) -> None:
    monkeypatch.setattr(activation, "operator_credential_readable", lambda: False)

    refusal = agent_activation_refusal(operation)

    assert refusal is not None
    assert refusal.startswith("spawned_agent_refused")


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_cleared_markers_with_operator_credential_pass(
    monkeypatch: pytest.MonkeyPatch, cleared_markers: None, operation: str
) -> None:
    monkeypatch.setattr(activation, "operator_credential_readable", lambda: True)

    assert agent_activation_refusal(operation) is None


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The account's passwd home, redirected to a temp directory; GOBBY_HOME and HOME point elsewhere.

    Returns the account's ``.gobby`` directory, where the operator bootstrap lives.
    """
    home = tmp_path / "account"
    home.mkdir()
    monkeypatch.setattr(local_token, "account_home", lambda: home)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "unused-gobby-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    gobby_dir = home / ".gobby"
    gobby_dir.mkdir()
    return gobby_dir


def test_missing_bootstrap_is_not_an_operator_credential(account: Path) -> None:
    assert activation.operator_credential_readable() is False


def test_bootstrap_behind_a_denied_directory_is_not_an_operator_credential(account: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads through a denied directory")
    (account / "bootstrap.yaml").write_text("database_url: postgresql://operator@127.0.0.1:1/hub\n")
    account.chmod(0o000)

    try:
        assert activation.operator_credential_readable() is False
    finally:
        account.chmod(0o700)


def test_unparseable_bootstrap_is_still_the_operator_file(account: Path) -> None:
    """A malformed bootstrap still belongs to the operator; the start path reports its error."""
    (account / "bootstrap.yaml").write_text("database_url: [unclosed\n")

    assert activation.operator_credential_readable() is True


def test_readable_bootstrap_is_the_operator_credential(account: Path) -> None:
    (account / "bootstrap.yaml").write_text("api_key: operator-key\n")

    assert activation.operator_credential_readable() is True


def test_a_redirected_gobby_home_is_never_the_operator_credential(
    account: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GOBBY_HOME selects a forged bootstrap; the account has none, so the credential is absent."""
    forged = tmp_path / "forged-home"
    forged.mkdir()
    (forged / "bootstrap.yaml").write_text("api_key: forged-by-caller\n")
    monkeypatch.setenv("GOBBY_HOME", str(forged))

    assert activation.operator_credential_readable() is False


def test_the_operator_credential_ignores_gobby_home_when_the_account_has_one(
    account: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (account / "bootstrap.yaml").write_text("api_key: operator-key\n")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "does-not-exist"))

    assert activation.operator_credential_readable() is True


def test_the_operator_credential_path_ignores_home(
    account: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HOME is an environment variable a caller can set; the canonical path must not follow it."""
    monkeypatch.setattr(local_token, "account_home", lambda: _REAL_ACCOUNT_HOME)
    monkeypatch.delenv("GOBBY_HOME", raising=False)
    redirected = tmp_path / "redirected-home"
    monkeypatch.setenv("HOME", str(redirected))

    path = local_token.operator_bootstrap_path()

    assert path is not None
    assert not path.is_relative_to(redirected)
    assert path == _REAL_ACCOUNT_HOME / ".gobby" / "bootstrap.yaml"


def test_an_account_without_a_passwd_entry_has_no_operator_credential(
    account: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail closed: when the account's home is unknown, no bootstrap is the operator's."""
    (tmp_path / "bootstrap.yaml").write_text("api_key: forged-by-caller\n")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(local_token, "account_home", lambda: None)

    assert activation.operator_credential_readable() is False
