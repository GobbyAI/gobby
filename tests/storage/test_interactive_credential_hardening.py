"""Unit coverage for interactive grant prune, protocol, and rotation rollback."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from gobby.storage.hub.protocol import Row, Transaction
from gobby.storage.managed_credentials import (
    CredentialIssuanceError,
    ManagedCredentialManager,
    RevocationOutcome,
)

pytestmark = pytest.mark.unit


class _BoomStore:
    def seal(self, plaintext: bytes, *, aad: bytes) -> str:
        raise RuntimeError("seal failed")

    def open_sealed(self, token: str, *, aad: bytes) -> bytes:
        raise AssertionError("open_sealed should not run")


class _FakeDatabase:
    def __init__(self, row: Mapping[str, object]) -> None:
        self._row = row

    @property
    def conninfo(self) -> str:
        return "postgresql://gobby@127.0.0.1/gobby"

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Row | None:
        return self._row

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Row]:
        return []

    def transaction(self) -> AbstractContextManager[Transaction]:
        txn = MagicMock()
        txn.execute.return_value.fetchone.return_value = self._row
        return nullcontext(txn)


class _RollbackManager(ManagedCredentialManager):
    def __init__(
        self,
        *,
        database: _FakeDatabase,
        machine_id: UUID,
        runtime_root: Path,
    ) -> None:
        super().__init__(
            database=database,
            machine_id=machine_id,
            runtime_root=runtime_root,
        )
        self.revoked_generations: list[int | None] = []

    def revoke_interactive(
        self,
        *,
        deployment_token: str,
        project_id: UUID,
        reason: str,
        generation: int | None = None,
    ) -> RevocationOutcome:
        self.revoked_generations.append(generation)
        return RevocationOutcome(completed=True, revoked_count=1)


def test_drain_until_discards_expired_grant_entries(tmp_path: Path) -> None:
    manager = ManagedCredentialManager(
        database=_FakeDatabase({}),
        machine_id=uuid4(),
        runtime_root=tmp_path,
    )
    token = "tokentokentoken"
    project_id = uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(minutes=10)
    manager._interactive_grant_expiry[(token, project_id, None, 1)] = past
    manager._interactive_grant_expiry[(token, project_id, None, 2)] = future

    until = manager._interactive_drain_until(token, project_id, None)

    assert until == future
    assert (token, project_id, None, 1) not in manager._interactive_grant_expiry
    assert manager._interactive_grant_expiry[(token, project_id, None, 2)] == future


def test_remember_interactive_grant_expiry_drops_past_entries(tmp_path: Path) -> None:
    manager = ManagedCredentialManager(
        database=_FakeDatabase({}),
        machine_id=uuid4(),
        runtime_root=tmp_path,
    )
    token = "tokentokentoken"
    project_id = uuid4()
    past = datetime.now(UTC) - timedelta(seconds=5)
    future = datetime.now(UTC) + timedelta(minutes=5)
    manager._interactive_grant_expiry[(token, project_id, None, 1)] = past

    manager.remember_interactive_grant_expiry(
        deployment_token=token,
        project_id=project_id,
        generation=2,
        expires_at=future,
    )

    assert (token, project_id, None, 1) not in manager._interactive_grant_expiry
    assert manager._interactive_grant_expiry[(token, project_id, None, 2)] == future


def test_rotate_interactive_rolls_back_successor_when_seal_fails(tmp_path: Path) -> None:
    execution_id = uuid4()
    manager = _RollbackManager(
        database=_FakeDatabase(
            {
                "role_name": "int_role",
                "credential_generation": 4,
                "managed_execution_id": execution_id,
            }
        ),
        machine_id=uuid4(),
        runtime_root=tmp_path,
    )

    with pytest.raises(CredentialIssuanceError, match="rotation failed"):
        manager.rotate_interactive(
            deployment_token="tokentokentoken",
            project_id=uuid4(),
            session_id=uuid4(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=_BoomStore(),
        )

    assert manager.revoked_generations == [4]


def test_interactive_secret_store_is_a_protocol() -> None:
    types_source = Path("src/gobby/storage/managed_credential_types.py").read_text(encoding="utf-8")
    assert "class SecretStore(Protocol)" in types_source
    for module in ("managed_credentials", "interactive_credentials"):
        source = Path(f"src/gobby/storage/{module}.py").read_text(encoding="utf-8")
        assert "secret_store: Any" not in source


def test_grant_expiry_memory_is_scoped_per_overlay(tmp_path: Path) -> None:
    manager = ManagedCredentialManager(
        database=_FakeDatabase({}),
        machine_id=uuid4(),
        runtime_root=tmp_path,
    )
    token = "tokentokentoken"
    project_id = uuid4()
    overlay_id = uuid4()
    main_expiry = datetime.now(UTC) + timedelta(minutes=30)
    overlay_expiry = datetime.now(UTC) + timedelta(minutes=10)
    manager.remember_interactive_grant_expiry(
        deployment_token=token,
        project_id=project_id,
        generation=1,
        expires_at=main_expiry,
    )
    manager.remember_interactive_grant_expiry(
        deployment_token=token,
        project_id=project_id,
        generation=2,
        expires_at=overlay_expiry,
        code_overlay_project_id=overlay_id,
    )

    assert manager._interactive_drain_until(token, project_id, overlay_id) == overlay_expiry
    assert manager._interactive_drain_until(token, project_id, None) == main_expiry
    manager._prune_interactive_grant_expiry(drop_before=(token, project_id, overlay_id, 3))
    assert (token, project_id, overlay_id, 2) not in manager._interactive_grant_expiry
    assert manager._interactive_grant_expiry[(token, project_id, None, 1)] == main_expiry
