"""Tests for canonical account identity storage."""

from __future__ import annotations

import pytest

from gobby.identity import hash_password, verify_password_hash
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.users import DuplicateUserEmailError, LocalUserManager
from tests.fixtures.postgres import (
    TEST_MACHINE_ID_PREFIX,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    TEST_USER_PASSWORD,
)

pytestmark = pytest.mark.unit

OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"
SEEDED_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


def test_create_lookup_list_and_profile_updates(temp_db: HubDatabase) -> None:
    manager = LocalUserManager(temp_db)
    created = manager.create(
        user_id=OTHER_USER_ID,
        name="  Other User  ",
        email="  Other@Example.COM  ",
        password_hash=hash_password("initial-password"),
    )

    assert created.id == OTHER_USER_ID
    assert created.name == "Other User"
    assert created.email == "Other@Example.COM"
    assert manager.get(OTHER_USER_ID) == created
    assert manager.get_by_email(" other@example.com ") == created
    assert {user.id for user in manager.list()} == {TEST_USER_ID, OTHER_USER_ID}

    updated = manager.update_profile(
        OTHER_USER_ID,
        name="Updated User",
        email="Updated@Example.com",
    )
    assert updated.name == "Updated User"
    assert updated.email == "Updated@Example.com"
    assert updated.updated_at >= created.updated_at


def test_duplicate_email_is_case_insensitive_and_typed(temp_db: HubDatabase) -> None:
    with pytest.raises(DuplicateUserEmailError, match="already exists"):
        LocalUserManager(temp_db).create(
            name="Duplicate",
            email=TEST_USER_EMAIL.upper(),
            password_hash=hash_password("different-password"),
        )


@pytest.mark.parametrize("field", ["name", "email"])
def test_create_rejects_blank_identity_fields(temp_db: HubDatabase, field: str) -> None:
    values = {"name": "Valid Name", "email": "valid@example.com"}
    values[field] = "   "

    with pytest.raises(ValueError, match="must not be blank"):
        LocalUserManager(temp_db).create(
            **values,
            password_hash=hash_password("password"),
        )


def test_password_hash_uses_random_canonical_argon2id_salts() -> None:
    first = hash_password(TEST_USER_PASSWORD)
    second = hash_password(TEST_USER_PASSWORD)

    assert first.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert second.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert first != second
    assert verify_password_hash(TEST_USER_PASSWORD, first) is True


def test_update_password_replaces_hash(temp_db: HubDatabase) -> None:
    manager = LocalUserManager(temp_db)
    before = manager.get(TEST_USER_ID)
    assert before is not None

    after = manager.update_password(TEST_USER_ID, hash_password("replacement-password"))

    assert after.password_hash != before.password_hash
    assert verify_password_hash("replacement-password", after.password_hash) is True


def test_resolve_for_session_uses_machine_owner(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: SEEDED_MACHINE_ID,
    )
    session = session_manager.register(
        external_id="user-resolution-session",
        machine_id=SEEDED_MACHINE_ID,
        source="test",
        project_id=None,
    )

    user = LocalUserManager(temp_db).resolve_for_session(session.id)

    assert user is not None
    assert user.id == TEST_USER_ID
