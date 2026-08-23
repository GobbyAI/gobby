"""Shared contracts for daemon-owned managed-execution credentials."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from gobby.storage.hub.protocol import Row, Transaction

AUTH_SCHEMA = "gobby_agent_auth"
MANAGED_EXECUTION_BOOTSTRAP_ENV = "GOBBY_MANAGED_EXECUTION_BOOTSTRAP"
# Bounds runaway lifetime requests while covering long-running agent spawns,
# whose credential lifetime derives from the run timeout (spawn timeout + 5min).
MAX_ROLE_LIFETIME = timedelta(hours=24)
DAEMON_LEASE_DURATION = timedelta(minutes=2)
REVOCATION_DRAIN_TIMEOUT_SECONDS = 5.0
REVOCATION_POLL_SECONDS = 0.05

OwnerKind = Literal["agent_run", "tool_chat", "interactive"]


class CredentialIssuanceError(RuntimeError):
    """A scoped role could not be issued and materialized safely."""


class CredentialAuthorizationError(ValueError):
    """A managed execution request does not match authoritative session scope."""


class GrantRevocationSink(Protocol):
    def revoke_execution(self, execution_id: UUID, generation: int | None) -> None: ...

    def revoke_interactive(
        self,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int | None,
    ) -> None: ...


class SecretStore(Protocol):
    """Seal and open interactive credential material."""

    def seal(self, plaintext: bytes, *, aad: bytes) -> str: ...

    def open_sealed(self, token: str, *, aad: bytes) -> bytes: ...


class _CredentialDatabase(Protocol):
    @property
    def conninfo(self) -> str: ...

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Row | None: ...

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Row]: ...

    def transaction(self) -> AbstractContextManager[Transaction]: ...


@dataclass(frozen=True)
class ManagedCredential:
    """Secret-free identity and lifecycle metadata for a scoped role."""

    managed_execution_id: UUID
    role_name: str
    credential_generation: int
    issued_at: datetime
    expires_at: datetime
    bootstrap_path: Path


@dataclass(frozen=True)
class RevocationOutcome:
    """Result of an idempotent managed-execution revocation request."""

    completed: bool
    revoked_count: int
    retry_recorded: bool = False
    failure_code: str | None = None


@dataclass(frozen=True)
class ManagedToolCredential:
    """Authoritative project scope and credential metadata for one tool request."""

    credential: ManagedCredential
    project_id: UUID
    project_path: str


@dataclass(frozen=True)
class MaintenanceCredential:
    """Project-scoped maintenance role plus the grant DSN for this execution."""

    credential: ManagedCredential
    dsn: str


@dataclass(frozen=True)
class InteractiveCredential:
    """Issued or reused interactive (machine, project) principal."""

    role_name: str
    credential_generation: int
    issued_at: datetime
    expires_at: datetime
    dsn: str
    reused: bool
    deployment_token: str
    machine_id: UUID
    project_id: UUID
    managed_execution_id: UUID
    code_overlay_project_id: UUID | None = None


def _row_value(row: Row, key: str) -> Any:
    return row[key]
