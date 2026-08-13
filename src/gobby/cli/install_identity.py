"""Transactional initial account and machine bootstrap."""

from __future__ import annotations

import platform
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import click

from gobby.identity import (
    hash_password,
    normalize_user_email,
    normalize_user_name,
    validate_password,
)
from gobby.storage.hub.protocol import Cursor, HubDatabase, Transaction
from gobby.storage.machines import LocalMachineManager
from gobby.storage.users import LocalUserManager, User, UserIdentityStateError
from gobby.utils.machine_id import require_machine_id


@dataclass(frozen=True)
class IdentityBootstrapMutation:
    """Serialize the one-time initial identity bootstrap."""

    PRIORITY: ClassVar[int] = 100


class InstallIdentityError(RuntimeError):
    """Raised when installation cannot establish one canonical local identity."""


class _TransactionDatabase:
    """Expose storage-manager read helpers over one transaction."""

    def __init__(self, transaction: Transaction) -> None:
        self._transaction = transaction

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        return self._transaction.execute(sql, params)

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Mapping[str, Any] | None:
        return self.execute(sql, params).fetchone()

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Mapping[str, Any]]:
        return self.execute(sql, params).fetchall()


def ensure_install_identity(db: HubDatabase, *, no_interactive: bool) -> User:
    """Create or verify the sole user and register this machine atomically."""
    existing = LocalUserManager(db).list()
    if len(existing) > 1:
        raise UserIdentityStateError(
            "Multiple users exist; v0.5 cannot select an installation account"
        )

    identity_input: tuple[str, str, str] | None = None
    if not existing:
        if no_interactive:
            raise InstallIdentityError(
                "No canonical user exists. Run `gobby install` interactively to create the account."
            )
        name = normalize_user_name(str(click.prompt("Name")))
        email = normalize_user_email(str(click.prompt("Email")))
        password = validate_password(
            str(click.prompt("Password", hide_input=True, confirmation_prompt=True))
        )
        identity_input = (name, email, hash_password(password))

    machine_id = require_machine_id()
    with db.transaction_immediate(IdentityBootstrapMutation()) as transaction:
        transaction_db = cast(HubDatabase, _TransactionDatabase(transaction))
        users = LocalUserManager(transaction_db)
        current = users.list()
        if len(current) > 1:
            raise UserIdentityStateError(
                "Multiple users exist; v0.5 cannot select an installation account"
            )
        if current:
            user = current[0]
        else:
            if identity_input is None:
                raise InstallIdentityError("Identity bootstrap input was not collected")
            name, email, password_hash = identity_input
            user = users.create(name=name, email=email, password_hash=password_hash)

        LocalMachineManager(transaction_db).upsert_seen(
            machine_id,
            user.id,
            hostname=socket.gethostname(),
            os=platform.system(),
        )
        return user
