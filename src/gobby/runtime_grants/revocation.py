"""Shared grant revocation ledger used by every request-scoped GrantService."""

from __future__ import annotations

import threading
from collections.abc import Callable
from uuid import UUID

from gobby.runtime_grants.schema import GrantBundle


class GrantRevocationStore:
    """Revocation state that outlives a single GrantService instance.

    Checksums record an explicit grant revoke. Principal keys record a
    production credential revoke, which never holds the issued grant object.
    An optional callback consults durable principal state (for example
    ``revoked_at``) so interactive revocation survives process restart.
    """

    def __init__(
        self,
        *,
        principal_revoked: Callable[[GrantBundle], bool] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._checksums: set[str] = set()
        self._executions: set[tuple[str, int | None]] = set()
        self._interactive: set[tuple[str, str, int | None]] = set()
        self._principal_revoked = principal_revoked

    def set_principal_revoked(self, check: Callable[[GrantBundle], bool]) -> None:
        self._principal_revoked = check

    def revoke_grant(self, grant: GrantBundle) -> None:
        with self._lock:
            self._checksums.add(grant.payload_checksum)

    def revoke_execution(self, execution_id: UUID | str, generation: int | None) -> None:
        with self._lock:
            self._executions.add((str(execution_id), generation))

    def revoke_interactive(
        self,
        *,
        deployment_token: str,
        project_id: UUID | str,
        generation: int | None,
    ) -> None:
        with self._lock:
            self._interactive.add((deployment_token, str(project_id), generation))

    def is_revoked(self, grant: GrantBundle) -> bool:
        generation = getattr(grant.capabilities.postgres, "credential_generation", None)
        if not isinstance(generation, int):
            generation = None
        execution_id = grant.principal.execution_id
        with self._lock:
            if grant.payload_checksum in self._checksums:
                return True
            if execution_id is not None and _key_revoked(
                self._executions, execution_id, generation
            ):
                return True
            if grant.principal.kind == "interactive" and _interactive_revoked(
                self._interactive,
                grant.deployment.token,
                grant.principal.project_id,
                generation,
            ):
                return True
        return self._principal_revoked is not None and self._principal_revoked(grant)


def _key_revoked(
    keys: set[tuple[str, int | None]],
    identity: str,
    generation: int | None,
) -> bool:
    if (identity, None) in keys:
        return True
    return generation is not None and (identity, generation) in keys


def _interactive_revoked(
    keys: set[tuple[str, str, int | None]],
    token: str,
    project_id: str,
    generation: int | None,
) -> bool:
    if (token, project_id, None) in keys:
        return True
    return generation is not None and (token, project_id, generation) in keys
