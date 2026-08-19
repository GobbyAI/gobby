"""Canonical account identity storage."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.errors import UniqueViolation

from gobby.identity import (
    normalize_user_email,
    normalize_user_name,
    validate_password_hash,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

_EMAIL_CONSTRAINT = "users_email_lower_key"


class DuplicateUserEmailError(ValueError):
    """Raised when a case-insensitive email identity already exists."""


class UserIdentityStateError(RuntimeError):
    """Raised when the datastore has no unambiguous installed user."""


@dataclass(frozen=True, slots=True)
class User:
    """Canonical account identity row."""

    id: str
    email: str
    name: str
    password_hash: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> User:
        return cls(
            id=str(row["id"]),
            email=str(row["email"]),
            name=str(row["name"]),
            password_hash=str(row["password_hash"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self, *, include_password_hash: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_password_hash:
            result["password_hash"] = self.password_hash
        return result


class LocalUserManager:
    """Manage canonical account identities in PostgreSQL."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def create(
        self,
        *,
        name: str,
        email: str,
        password_hash: str,
        user_id: str | None = None,
    ) -> User:
        normalized_id = (
            str(uuid.UUID(user_id.strip())) if user_id is not None else str(uuid.uuid4())
        )
        normalized_name = normalize_user_name(name)
        normalized_email = normalize_user_email(email)
        normalized_hash = validate_password_hash(password_hash)
        try:
            row = self.db.execute(
                """
                INSERT INTO users (id, email, name, password_hash)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (
                    normalized_id,
                    normalized_email,
                    normalized_name,
                    normalized_hash,
                ),
            ).fetchone()
        except UniqueViolation as exc:
            self._raise_duplicate_email(exc, normalized_email)
            raise
        if row is None:
            raise RuntimeError("User insert returned no row")
        return User.from_row(row)

    def get(self, user_id: str) -> User | None:
        normalized_id = str(uuid.UUID(user_id.strip()))
        row = self.db.fetchone("SELECT * FROM users WHERE id = %s", (normalized_id,))
        return User.from_row(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        normalized_email = normalize_user_email(email)
        row = self.db.fetchone(
            "SELECT * FROM users WHERE lower(email) = lower(%s)",
            (normalized_email,),
        )
        return User.from_row(row) if row else None

    def list(self) -> list[User]:
        rows = self.db.fetchall("SELECT * FROM users ORDER BY created_at, id")
        return [User.from_row(row) for row in rows]

    def require_sole_user(self) -> User:
        users = self.list()
        if len(users) != 1:
            raise UserIdentityStateError(
                f"Expected exactly one installed user, found {len(users)}; "
                "run gobby install to repair identity"
            )
        return users[0]

    def update_profile(self, user_id: str, *, name: str, email: str) -> User:
        normalized_id = str(uuid.UUID(user_id.strip()))
        normalized_name = normalize_user_name(name)
        normalized_email = normalize_user_email(email)
        try:
            row = self.db.execute(
                """
                UPDATE users
                SET name = %s, email = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (normalized_name, normalized_email, utc_now(), normalized_id),
            ).fetchone()
        except UniqueViolation as exc:
            self._raise_duplicate_email(exc, normalized_email)
            raise
        if row is None:
            raise KeyError(f"Unknown user: {normalized_id}")
        return User.from_row(row)

    def update_password(self, user_id: str, password_hash: str) -> User:
        normalized_id = str(uuid.UUID(user_id.strip()))
        normalized_hash = validate_password_hash(password_hash)
        with self.db.transaction() as transaction:
            row = transaction.execute(
                """
                UPDATE users
                SET password_hash = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (normalized_hash, utc_now(), normalized_id),
            ).fetchone()
            if row is not None:
                transaction.execute(
                    "DELETE FROM auth_sessions WHERE user_id = %s", (normalized_id,)
                )
        if row is None:
            raise KeyError(f"Unknown user: {normalized_id}")
        return User.from_row(row)

    def resolve_for_session(self, session_id: str) -> User | None:
        normalized_session_id = str(uuid.UUID(session_id.strip()))
        row = self.db.fetchone(
            """
            SELECT users.*
            FROM sessions
            JOIN machines ON machines.id = sessions.machine_id
            JOIN users ON users.id = machines.owner_user_id
            WHERE sessions.id = %s
            """,
            (normalized_session_id,),
        )
        return User.from_row(row) if row else None

    @staticmethod
    def _raise_duplicate_email(exc: UniqueViolation, email: str) -> None:
        if exc.diag.constraint_name == _EMAIL_CONSTRAINT:
            raise DuplicateUserEmailError(f"User email already exists: {email}") from exc
