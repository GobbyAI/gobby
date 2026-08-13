"""Tests for transactional install-time identity bootstrap."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from gobby.cli.install_identity import InstallIdentityError, ensure_install_identity
from gobby.identity import verify_password_hash
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.users import LocalUserManager
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000099"


@pytest.fixture
def empty_identity(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    temp_db.execute("DELETE FROM auth_sessions")
    temp_db.execute("DELETE FROM machines")
    temp_db.execute("DELETE FROM users")
    yield temp_db


def test_first_install_creates_user_and_owned_machine(
    empty_identity: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["  Test Operator  ", "  Operator@Example.COM  ", "password"])
    monkeypatch.setattr(
        "gobby.cli.install_identity.click.prompt", lambda *args, **kwargs: next(answers)
    )
    monkeypatch.setattr("gobby.cli.install_identity.require_machine_id", lambda: MACHINE_ID)

    user = ensure_install_identity(empty_identity, no_interactive=False)

    assert user.name == "Test Operator"
    assert user.email == "Operator@Example.COM"
    assert verify_password_hash("password", user.password_hash) is True
    machine = LocalMachineManager(empty_identity).get(MACHINE_ID)
    assert machine is not None
    assert machine.owner_user_id == user.id


def test_machine_failure_rolls_back_new_user(
    empty_identity: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["Test Operator", "operator@example.com", "password"])
    monkeypatch.setattr(
        "gobby.cli.install_identity.click.prompt", lambda *args, **kwargs: next(answers)
    )
    monkeypatch.setattr("gobby.cli.install_identity.require_machine_id", lambda: MACHINE_ID)

    def fail_machine_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("machine insert failed")

    monkeypatch.setattr(
        "gobby.cli.install_identity.LocalMachineManager.upsert_seen",
        fail_machine_insert,
    )

    with pytest.raises(RuntimeError, match="machine insert failed"):
        ensure_install_identity(empty_identity, no_interactive=False)

    assert LocalUserManager(empty_identity).list() == []


def test_unattended_first_install_fails_with_guidance(empty_identity: HubDatabase) -> None:
    with pytest.raises(InstallIdentityError, match="interactively"):
        ensure_install_identity(empty_identity, no_interactive=True)


def test_rerun_uses_sole_user_without_prompts(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.cli.install_identity.require_machine_id", lambda: MACHINE_ID)
    monkeypatch.setattr(
        "gobby.cli.install_identity.click.prompt",
        lambda *args, **kwargs: pytest.fail("rerun prompted for identity"),
    )

    first = ensure_install_identity(temp_db, no_interactive=True)
    second = ensure_install_identity(temp_db, no_interactive=True)

    assert first.id == TEST_USER_ID
    assert second.id == TEST_USER_ID
    machines = LocalMachineManager(temp_db).list_for_user(TEST_USER_ID)
    assert [machine.id for machine in machines].count(MACHINE_ID) == 1
