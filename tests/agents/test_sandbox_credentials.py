"""The sandbox denies the operator's canonical bootstrap even when GOBBY_HOME redirects the daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents.sandbox_credentials import credential_roots
from gobby.utils import local_token

pytestmark = pytest.mark.unit


def test_credential_roots_deny_canonical_operator_credentials_under_a_gobby_home_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account = tmp_path / "account"
    monkeypatch.setattr(local_token, "account_home", lambda: account)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "override-home"))

    roots = credential_roots()
    assert account / ".gobby" / "bootstrap.yaml" in roots
    assert account / ".gobby" / "break_glass" in roots
    assert account / ".gobby" / ".break_glass-staging" in roots
    # The Python oracle keeps the runtime-home pair; the Rust side drops it and
    # receives this pair through the hub-sent authority roots instead.
    assert tmp_path / "override-home" / "break_glass" in roots
    assert tmp_path / "override-home" / ".break_glass-staging" in roots


def test_credential_roots_deny_runtime_home_break_glass_when_bound_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(local_token, "account_home", lambda: tmp_path / "account")
    monkeypatch.setattr(
        local_token, "_daemon_bootstrap", tmp_path / "bound-daemon" / "startup.yaml"
    )
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "override-home"))

    roots = credential_roots()

    assert tmp_path / "override-home" / "break_glass" in roots
    assert tmp_path / "override-home" / ".break_glass-staging" in roots
    assert tmp_path / "bound-daemon" / "break_glass" in roots
    assert tmp_path / "bound-daemon" / ".break_glass-staging" in roots


def test_credential_roots_survive_an_account_without_a_passwd_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(local_token, "account_home", lambda: None)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "override-home"))

    roots = credential_roots()

    assert tmp_path / "override-home" / "bootstrap.yaml" in roots
    assert all(isinstance(root, Path) for root in roots)


def test_credential_roots_deny_the_capability_signing_key_and_its_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(local_token, "account_home", lambda: None)

    roots = credential_roots()

    assert home / "capability_signing_key" in roots
    assert home / ".capability_signing_key-staging" in roots


def test_credential_roots_deny_the_key_beside_a_bound_daemon_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    monkeypatch.setattr(local_token, "account_home", lambda: None)
    bound = tmp_path / "bound" / "bootstrap.yaml"
    monkeypatch.setattr(local_token, "_daemon_bootstrap", bound)

    roots = credential_roots()

    assert bound.with_name("capability_signing_key") in roots
    assert bound.with_name(".capability_signing_key-staging") in roots


def test_credential_roots_deny_the_session_credential_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(local_token, "account_home", lambda: None)

    assert home / "session-credentials" in credential_roots()
