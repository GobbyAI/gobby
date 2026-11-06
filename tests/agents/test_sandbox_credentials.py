"""The sandbox denies the operator's canonical bootstrap even when GOBBY_HOME redirects the daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents.sandbox_credentials import credential_roots
from gobby.utils import local_token

pytestmark = pytest.mark.unit


def test_credential_roots_deny_the_canonical_operator_bootstrap_under_a_gobby_home_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account = tmp_path / "account"
    monkeypatch.setattr(local_token, "account_home", lambda: account)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "override-home"))

    assert account / ".gobby" / "bootstrap.yaml" in credential_roots()


def test_credential_roots_refuse_an_account_without_a_passwd_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(local_token, "account_home", lambda: None)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "override-home"))

    with pytest.raises(RuntimeError, match="passwd home is unknown"):
        credential_roots()
