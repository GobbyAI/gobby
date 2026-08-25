"""Interactive (deployment token, machine, project, overlay) principal lifecycle."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from gobby.storage.hub.protocol import Transaction
from gobby.storage.managed_credential_types import (
    AUTH_SCHEMA,
    CredentialAuthorizationError,
    CredentialIssuanceError,
    GrantRevocationSink,
    InteractiveCredential,
    RevocationOutcome,
    SecretStore,
    _CredentialDatabase,
    _row_value,
)

InteractiveGrantExpiryKey = tuple[str, UUID, UUID | None, int]
# SQLSTATEs the issuer raises when a requested overlay is not a registered isolation
# workspace of the session project (23503) or is the project itself (22023).
_OVERLAY_REJECTION_SQLSTATES = frozenset({"23503", "22023"})


class _InteractiveCredentialHost(Protocol):
    _database: _CredentialDatabase
    _machine_id: UUID
    _grant_revocations: GrantRevocationSink | None
    _interactive_grant_expiry: dict[InteractiveGrantExpiryKey, datetime]

    def heartbeat(self) -> None: ...

    def revoke(
        self,
        managed_execution_id: UUID,
        *,
        generation: int | None = None,
        reason: str,
    ) -> RevocationOutcome: ...

    def _interactive_dsn(self, role_name: str, password: str) -> str: ...

    @staticmethod
    def _validate_expiry(issued_at: datetime, expires_at: datetime) -> datetime: ...

    def _prune_interactive_grant_expiry(
        self, *, drop_before: InteractiveGrantExpiryKey | None = None
    ) -> None: ...

    def _interactive_drain_until(
        self, deployment_token: str, project_id: UUID, code_overlay_project_id: UUID | None
    ) -> datetime: ...

    def _interactive_aad(
        self, *, deployment_token: str, project_id: UUID, generation: int
    ) -> str: ...

    def _store_interactive_password(
        self,
        secret_store: SecretStore,
        txn: Transaction,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
        password: str,
    ) -> None: ...

    def _load_interactive_password(
        self,
        secret_store: SecretStore,
        txn: Transaction,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
    ) -> str: ...

    def revoke_interactive(
        self,
        *,
        deployment_token: str,
        project_id: UUID,
        reason: str,
        generation: int | None = None,
    ) -> RevocationOutcome: ...


class InteractiveCredentialMixin:
    """Issue, reuse, rotate, and revoke interactive principals for a credential manager."""

    def issue_interactive(
        self: _InteractiveCredentialHost,
        *,
        deployment_token: str,
        project_id: UUID,
        session_id: UUID | None,
        expires_at: datetime,
        secret_store: SecretStore,
        code_overlay_project_id: UUID | None = None,
    ) -> InteractiveCredential:
        issued_at = datetime.now(UTC)
        normalized_expiry = self._validate_expiry(issued_at, expires_at)
        self.heartbeat()
        password = secrets.token_urlsafe(32)
        try:
            # One transaction: issue_or_reuse_interactive_principal takes an advisory
            # xact lock, so holding the transaction open until the sealed material is
            # written means a concurrent issuer can never reuse a binding whose
            # material does not exist yet.
            with self._database.transaction() as txn:
                row = txn.execute(
                    f"""SELECT * FROM {AUTH_SCHEMA}.issue_or_reuse_interactive_principal(
                        %s, %s, %s, %s, %s, %s, %s
                    )""",
                    (
                        deployment_token,
                        self._machine_id,
                        project_id,
                        session_id,
                        normalized_expiry,
                        password,
                        code_overlay_project_id,
                    ),
                ).fetchone()
                if row is None:
                    raise CredentialIssuanceError("interactive issuance returned no result")
                role_name = str(_row_value(row, "role_name"))
                generation = int(_row_value(row, "credential_generation"))
                reused = bool(_row_value(row, "reused"))
                execution_id = UUID(str(_row_value(row, "managed_execution_id")))
                if reused:
                    password = self._load_interactive_password(
                        secret_store,
                        txn,
                        deployment_token=deployment_token,
                        project_id=project_id,
                        generation=generation,
                    )
                else:
                    self._store_interactive_password(
                        secret_store,
                        txn,
                        deployment_token=deployment_token,
                        project_id=project_id,
                        generation=generation,
                        password=password,
                    )
            return InteractiveCredential(
                role_name=role_name,
                credential_generation=generation,
                issued_at=issued_at,
                expires_at=normalized_expiry,
                dsn=self._interactive_dsn(role_name, password),
                reused=reused,
                deployment_token=deployment_token,
                machine_id=self._machine_id,
                project_id=project_id,
                managed_execution_id=execution_id,
                code_overlay_project_id=code_overlay_project_id,
            )
        except Exception as error:
            if isinstance(error, CredentialIssuanceError):
                raise
            _raise_if_overlay_rejected(error)
            raise CredentialIssuanceError("interactive credential issuance failed") from error
        finally:
            password = ""

    def rotate_interactive(
        self: _InteractiveCredentialHost,
        *,
        deployment_token: str,
        project_id: UUID,
        session_id: UUID | None,
        expires_at: datetime,
        secret_store: SecretStore,
        code_overlay_project_id: UUID | None = None,
    ) -> InteractiveCredential:
        issued_at = datetime.now(UTC)
        normalized_expiry = self._validate_expiry(issued_at, expires_at)
        self.heartbeat()
        password = secrets.token_urlsafe(32)
        generation: int | None = None
        try:
            drain_until = self._interactive_drain_until(
                deployment_token, project_id, code_overlay_project_id
            )
            with self._database.transaction() as txn:
                row = txn.execute(
                    f"""SELECT * FROM {AUTH_SCHEMA}.rotate_interactive_principal(
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )""",
                    (
                        deployment_token,
                        self._machine_id,
                        project_id,
                        session_id,
                        normalized_expiry,
                        password,
                        drain_until,
                        code_overlay_project_id,
                    ),
                ).fetchone()
                if row is None:
                    raise CredentialIssuanceError("interactive rotation returned no result")
                role_name = str(_row_value(row, "role_name"))
                generation = int(_row_value(row, "credential_generation"))
                execution_id = UUID(str(_row_value(row, "managed_execution_id")))
                self._store_interactive_password(
                    secret_store,
                    txn,
                    deployment_token=deployment_token,
                    project_id=project_id,
                    generation=generation,
                    password=password,
                )
            credential = InteractiveCredential(
                role_name=role_name,
                credential_generation=generation,
                issued_at=issued_at,
                expires_at=normalized_expiry,
                dsn=self._interactive_dsn(role_name, password),
                reused=False,
                deployment_token=deployment_token,
                machine_id=self._machine_id,
                project_id=project_id,
                managed_execution_id=execution_id,
                code_overlay_project_id=code_overlay_project_id,
            )
            self._prune_interactive_grant_expiry(
                drop_before=(deployment_token, project_id, code_overlay_project_id, generation),
            )
            return credential
        except Exception as error:
            if generation is not None:
                self.revoke_interactive(
                    deployment_token=deployment_token,
                    project_id=project_id,
                    generation=generation,
                    reason="rotation-rollback",
                )
            if isinstance(error, CredentialIssuanceError):
                raise
            _raise_if_overlay_rejected(error)
            raise CredentialIssuanceError("interactive credential rotation failed") from error
        finally:
            password = ""

    def revoke_interactive(
        self: _InteractiveCredentialHost,
        *,
        deployment_token: str,
        project_id: UUID,
        reason: str,
        generation: int | None = None,
    ) -> RevocationOutcome:
        row = self._database.fetchone(
            f"""SELECT * FROM {AUTH_SCHEMA}.lookup_interactive_principal(
                %s::text, %s::uuid, %s::uuid, %s::integer
            )""",
            (deployment_token, self._machine_id, project_id, generation),
        )
        if row is None:
            return RevocationOutcome(completed=True, revoked_count=0)
        execution_id = UUID(str(_row_value(row, "managed_execution_id")))
        outcome = self.revoke(execution_id, generation=generation, reason=reason)
        if outcome.completed and self._grant_revocations is not None:
            self._grant_revocations.revoke_interactive(
                deployment_token=deployment_token,
                project_id=project_id,
                generation=generation,
            )
        return outcome

    def remember_interactive_grant_expiry(
        self: _InteractiveCredentialHost,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
        expires_at: datetime,
        code_overlay_project_id: UUID | None = None,
    ) -> None:
        self._prune_interactive_grant_expiry()
        key = (deployment_token, project_id, code_overlay_project_id, generation)
        current = self._interactive_grant_expiry.get(key)
        if current is None or expires_at > current:
            self._interactive_grant_expiry[key] = expires_at

    def interactive_generation_revoked(
        self: _InteractiveCredentialHost,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
    ) -> bool:
        row = self._database.fetchone(
            f"""SELECT * FROM {AUTH_SCHEMA}.lookup_interactive_principal(
                %s::text, %s::uuid, %s::uuid, %s::integer
            )""",
            (deployment_token, self._machine_id, project_id, generation),
        )
        if row is None:
            return True
        revoked_at = _row_value(row, "revoked_at")
        return revoked_at is not None

    def _prune_interactive_grant_expiry(
        self: _InteractiveCredentialHost,
        *,
        drop_before: InteractiveGrantExpiryKey | None = None,
    ) -> None:
        now = datetime.now(UTC)
        stale = [
            key
            for key, expiry in self._interactive_grant_expiry.items()
            if expiry <= now
            or (drop_before is not None and key[:3] == drop_before[:3] and key[3] < drop_before[3])
        ]
        for key in stale:
            del self._interactive_grant_expiry[key]

    def _interactive_drain_until(
        self: _InteractiveCredentialHost,
        deployment_token: str,
        project_id: UUID,
        code_overlay_project_id: UUID | None,
    ) -> datetime:
        self._prune_interactive_grant_expiry()
        scope = (deployment_token, project_id, code_overlay_project_id)
        matching = [
            expiry for key, expiry in self._interactive_grant_expiry.items() if key[:3] == scope
        ]
        if matching:
            return max(matching)
        return datetime.now(UTC) + timedelta(minutes=5)

    def _interactive_aad(
        self: _InteractiveCredentialHost,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
    ) -> str:
        return f"{deployment_token}:{self._machine_id}:{project_id}:{generation}"

    def _store_interactive_password(
        self: _InteractiveCredentialHost,
        secret_store: SecretStore,
        txn: Transaction,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
        password: str,
    ) -> None:
        aad = self._interactive_aad(
            deployment_token=deployment_token,
            project_id=project_id,
            generation=generation,
        )
        ciphertext = secret_store.seal(password.encode("utf-8"), aad=aad.encode("utf-8"))
        stored = txn.execute(
            f"""SELECT {AUTH_SCHEMA}.replace_interactive_credential_material(
                %s, %s, %s, %s, %s, %s
            )""",
            (
                deployment_token,
                self._machine_id,
                project_id,
                generation,
                ciphertext,
                aad,
            ),
        ).fetchone()
        if stored is None or not bool(
            _row_value(stored, "replace_interactive_credential_material")
        ):
            raise CredentialIssuanceError(
                "interactive credential material was not stored for a live binding"
            )

    def _load_interactive_password(
        self: _InteractiveCredentialHost,
        secret_store: SecretStore,
        txn: Transaction,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
    ) -> str:
        row = txn.execute(
            f"""SELECT * FROM {AUTH_SCHEMA}.load_interactive_credential_material(
                %s, %s, %s, %s
            )""",
            (deployment_token, self._machine_id, project_id, generation),
        ).fetchone()
        if row is None:
            raise CredentialIssuanceError("interactive credential material is missing")
        ciphertext = str(_row_value(row, "ciphertext"))
        aad = str(_row_value(row, "aad_identity"))
        plaintext = secret_store.open_sealed(ciphertext, aad=aad.encode("utf-8"))
        if not isinstance(plaintext, bytes):
            raise CredentialIssuanceError("interactive credential material is malformed")
        return plaintext.decode("utf-8")

    def _interactive_dsn(self: _InteractiveCredentialHost, role_name: str, password: str) -> str:
        parsed = conninfo_to_dict(self._database.conninfo)
        parsed.update(user=role_name, password=password)
        return make_conninfo("", **parsed)


def _raise_if_overlay_rejected(error: BaseException) -> None:
    """Surface issuer refusals of the requested overlay as authorization errors."""
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in _OVERLAY_REJECTION_SQLSTATES:
        diagnostic = getattr(error, "diag", None)
        message = getattr(diagnostic, "message_primary", None) or str(error)
        raise CredentialAuthorizationError(message) from error
