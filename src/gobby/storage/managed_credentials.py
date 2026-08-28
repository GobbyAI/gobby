"""Daemon-owned lifecycle for scoped managed-execution database credentials."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.interactive_credentials import (
    InteractiveCredentialMixin,
    InteractiveGrantExpiryKey,
)
from gobby.storage.managed_credential_types import (
    AUTH_SCHEMA,
    DAEMON_LEASE_DURATION,
    MANAGED_EXECUTION_BOOTSTRAP_ENV,
    MAX_ROLE_LIFETIME,
    REVOCATION_DRAIN_TIMEOUT_SECONDS,
    REVOCATION_POLL_SECONDS,
    CredentialAuthorizationError,
    CredentialIssuanceError,
    GrantRevocationSink,
    InteractiveCredential,
    MaintenanceCredential,
    ManagedCredential,
    ManagedToolCredential,
    OwnerKind,
    RevocationOutcome,
    SecretStore,
    _CredentialDatabase,
    _row_value,
    auth_schema_for,
    resolve_auth_schema,
)

__all__ = [
    "AUTH_SCHEMA",
    "auth_schema_for",
    "resolve_auth_schema",
    "DAEMON_LEASE_DURATION",
    "MANAGED_EXECUTION_BOOTSTRAP_ENV",
    "MAX_ROLE_LIFETIME",
    "REVOCATION_DRAIN_TIMEOUT_SECONDS",
    "REVOCATION_POLL_SECONDS",
    "CredentialAuthorizationError",
    "CredentialIssuanceError",
    "GrantRevocationSink",
    "InteractiveCredential",
    "MaintenanceCredential",
    "ManagedCredential",
    "ManagedCredentialManager",
    "ManagedToolCredential",
    "OwnerKind",
    "RevocationOutcome",
    "SecretStore",
]


class ManagedCredentialManager(InteractiveCredentialMixin):
    """Own scoped role issuance, private bootstrap files, and revocation retries."""

    def __init__(
        self,
        *,
        database: HubDatabase | _CredentialDatabase,
        machine_id: UUID,
        runtime_root: Path,
        owns_database: bool = False,
    ) -> None:
        self._database = database
        self._machine_id = machine_id
        self._runtime_root = runtime_root
        self._owns_database = owns_database
        self._interactive_grant_expiry: dict[InteractiveGrantExpiryKey, datetime] = {}
        self._grant_revocations: GrantRevocationSink | None = None
        self._auth_schema: str | None = None

    @property
    def auth_schema(self) -> str:
        """Per-hub agent-auth schema, resolved once from the connection's schema."""
        if self._auth_schema is None:
            self._auth_schema = resolve_auth_schema(self._database)
        return self._auth_schema

    def bind_grant_revocations(self, sink: GrantRevocationSink) -> None:
        self._grant_revocations = sink

    def close(self) -> None:
        if self._owns_database and hasattr(self._database, "close"):
            cast(HubDatabase, self._database).close()

    def heartbeat(self) -> None:
        row = self._database.fetchone(
            f"SELECT {self.auth_schema}.heartbeat_daemon(%s, %s)",
            (self._machine_id, DAEMON_LEASE_DURATION),
        )
        if row is None:
            raise RuntimeError("daemon credential heartbeat returned no result")

    def issue(
        self,
        *,
        managed_execution_id: UUID,
        owner_kind: OwnerKind,
        session_id: UUID,
        agent_run_id: UUID | None,
        expires_at: datetime,
    ) -> ManagedCredential:
        issued_at = datetime.now(UTC)
        normalized_expiry = self._validate_expiry(issued_at, expires_at)
        self.heartbeat()
        password = secrets.token_urlsafe(32)
        role_name: str | None = None
        generation: int | None = None
        try:
            if owner_kind == "tool_chat":
                row = self._database.fetchone(
                    f"""SELECT * FROM {self.auth_schema}.issue_tool_principal(
                        %s, %s, %s, %s, %s
                    )""",
                    (
                        managed_execution_id,
                        session_id,
                        self._machine_id,
                        normalized_expiry,
                        password,
                    ),
                )
            else:
                row = self._database.fetchone(
                    f"""SELECT * FROM {self.auth_schema}.issue_principal(
                        %s, %s, %s, %s, %s, %s, %s
                    )""",
                    (
                        managed_execution_id,
                        owner_kind,
                        session_id,
                        agent_run_id,
                        self._machine_id,
                        normalized_expiry,
                        password,
                    ),
                )
            if row is None:
                raise CredentialIssuanceError("managed credential issuance returned no result")
            role_name = str(_row_value(row, "role_name"))
            generation = int(_row_value(row, "credential_generation"))
            scoped_dsn = self._scoped_dsn(role_name, password, managed_execution_id)
            bootstrap_path = self._materialize_bootstrap(
                managed_execution_id=managed_execution_id,
                role_name=role_name,
                generation=generation,
                expires_at=normalized_expiry,
                scoped_dsn=scoped_dsn,
            )
            return ManagedCredential(
                managed_execution_id=managed_execution_id,
                role_name=role_name,
                credential_generation=generation,
                issued_at=issued_at,
                expires_at=normalized_expiry,
                bootstrap_path=bootstrap_path,
            )
        except Exception as error:
            if role_name is not None and generation is not None:
                self.revoke(
                    managed_execution_id,
                    generation=generation,
                    reason="issuance-rollback",
                )
            if isinstance(error, CredentialIssuanceError):
                raise
            raise CredentialIssuanceError("managed credential issuance failed") from error
        finally:
            password = ""

    def issue_maintenance(
        self,
        *,
        managed_execution_id: UUID,
        project_id: UUID,
        expires_at: datetime,
        code_overlay_project_id: UUID | None = None,
    ) -> MaintenanceCredential:
        issued_at = datetime.now(UTC)
        normalized_expiry = self._validate_expiry(issued_at, expires_at)
        self.heartbeat()
        password = secrets.token_urlsafe(32)
        role_name: str | None = None
        generation: int | None = None
        try:
            row = self._database.fetchone(
                f"""SELECT * FROM {self.auth_schema}.issue_maintenance_principal(
                    %s, %s, %s, %s, %s, %s
                )""",
                (
                    managed_execution_id,
                    project_id,
                    self._machine_id,
                    normalized_expiry,
                    password,
                    code_overlay_project_id,
                ),
            )
            if row is None:
                raise CredentialIssuanceError("maintenance credential issuance returned no result")
            role_name = str(_row_value(row, "role_name"))
            generation = int(_row_value(row, "credential_generation"))
            scoped_dsn = self._scoped_dsn(role_name, password, managed_execution_id)
            bootstrap_path = self._materialize_bootstrap(
                managed_execution_id=managed_execution_id,
                role_name=role_name,
                generation=generation,
                expires_at=normalized_expiry,
                scoped_dsn=scoped_dsn,
            )
            return MaintenanceCredential(
                credential=ManagedCredential(
                    managed_execution_id=managed_execution_id,
                    role_name=role_name,
                    credential_generation=generation,
                    issued_at=issued_at,
                    expires_at=normalized_expiry,
                    bootstrap_path=bootstrap_path,
                ),
                dsn=scoped_dsn,
            )
        except Exception as error:
            if role_name is not None and generation is not None:
                self.revoke(
                    managed_execution_id,
                    generation=generation,
                    reason="issuance-rollback",
                )
            if isinstance(error, CredentialIssuanceError):
                raise
            raise CredentialIssuanceError("maintenance credential issuance failed") from error
        finally:
            password = ""

    def revoke(
        self,
        managed_execution_id: UUID,
        *,
        generation: int | None = None,
        reason: str,
    ) -> RevocationOutcome:
        try:
            deadline = time.monotonic() + REVOCATION_DRAIN_TIMEOUT_SECONDS
            while True:
                row = self._database.fetchone(
                    f"SELECT {self.auth_schema}.revoke_principal(%s, %s)",
                    (managed_execution_id, generation),
                )
                if row is None:
                    raise RuntimeError("managed credential revocation returned no result")
                revoked_count = int(_row_value(row, "revoke_principal"))
                if revoked_count >= 0 or time.monotonic() >= deadline:
                    break
                time.sleep(REVOCATION_POLL_SECONDS)
        except Exception as error:
            self._delete_bootstrap(managed_execution_id, generation)
            failure_code = type(error).__name__
            self._write_retry_record(
                managed_execution_id,
                generation,
                reason,
                failure_code=failure_code,
            )
            return RevocationOutcome(
                completed=False,
                revoked_count=0,
                retry_recorded=True,
                failure_code=failure_code,
            )

        if revoked_count < 0:
            self._delete_bootstrap(managed_execution_id, generation)
            self._write_retry_record(
                managed_execution_id,
                generation,
                reason,
                failure_code="active_sessions_remaining",
            )
            return RevocationOutcome(
                completed=False,
                revoked_count=0,
                retry_recorded=True,
                failure_code="active_sessions_remaining",
            )

        self._delete_bootstrap(managed_execution_id, generation)
        self._delete_retry_record(managed_execution_id)
        if self._grant_revocations is not None:
            self._grant_revocations.revoke_execution(managed_execution_id, generation)
        return RevocationOutcome(completed=True, revoked_count=revoked_count)

    def issue_tool_request(
        self,
        *,
        session_id: UUID,
        requested_project_path: str,
        expires_at: datetime,
    ) -> ManagedToolCredential:
        resolved = self._database.fetchone(
            f"SELECT * FROM {self.auth_schema}.resolve_tool_session(%s)",
            (session_id,),
        )
        if resolved is None:
            raise CredentialAuthorizationError("authenticated session is unavailable")
        project_id = UUID(str(_row_value(resolved, "project_id")))
        authoritative_path = Path(str(_row_value(resolved, "repo_path"))).resolve()
        requested_path = Path(requested_project_path).resolve()
        if requested_path != authoritative_path:
            raise CredentialAuthorizationError("authenticated session project path mismatch")
        credential = self.issue(
            managed_execution_id=uuid4(),
            owner_kind="tool_chat",
            session_id=session_id,
            agent_run_id=None,
            expires_at=expires_at,
        )
        return ManagedToolCredential(
            credential=credential,
            project_id=project_id,
            project_path=str(authoritative_path),
        )

    def rotate_due(self) -> list[ManagedCredential]:
        self.heartbeat()
        due = self._database.fetchall(
            f"SELECT * FROM {self.auth_schema}.principals_due_for_rotation(%s)",
            (self._machine_id,),
        )
        rotated_credentials: list[ManagedCredential] = []
        for candidate in due:
            execution_id = cast(UUID, _row_value(candidate, "managed_execution_id"))
            predecessor_generation = int(_row_value(candidate, "credential_generation"))
            password = secrets.token_urlsafe(32)
            issued_at = datetime.now(UTC)
            expires_at = issued_at + timedelta(minutes=59)
            successor_generation: int | None = None
            try:
                row = self._database.fetchone(
                    f"""SELECT * FROM {self.auth_schema}.rotate_principal_if_generation(
                        %s, %s, %s, %s
                    )""",
                    (execution_id, predecessor_generation, expires_at, password),
                )
                if row is None:
                    continue
                role_name = str(_row_value(row, "role_name"))
                successor_generation = int(_row_value(row, "credential_generation"))
                scoped_dsn = self._scoped_dsn(role_name, password, execution_id)
                bootstrap_path = self._materialize_bootstrap(
                    managed_execution_id=execution_id,
                    role_name=role_name,
                    generation=successor_generation,
                    expires_at=expires_at,
                    scoped_dsn=scoped_dsn,
                )
                credential = ManagedCredential(
                    managed_execution_id=execution_id,
                    role_name=role_name,
                    credential_generation=successor_generation,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    bootstrap_path=bootstrap_path,
                )
            except Exception as error:
                if successor_generation is not None:
                    self.revoke(
                        execution_id,
                        generation=successor_generation,
                        reason="rotation-rollback",
                    )
                    self._database.fetchone(
                        f"SELECT {self.auth_schema}.cancel_principal_rotation(%s, %s, %s)",
                        (execution_id, predecessor_generation, successor_generation),
                    )
                raise CredentialIssuanceError("managed credential rotation failed") from error
            finally:
                password = ""

            self.revoke(
                execution_id,
                generation=predecessor_generation,
                reason="rotation-predecessor",
            )
            rotated_credentials.append(credential)
        return rotated_credentials

    def list_active(self) -> list[dict[str, object]]:
        """Return active scoped-role metadata without credential material."""
        rows = self._database.fetchall(f"SELECT * FROM {self.auth_schema}.list_active_principals()")
        results: list[dict[str, object]] = []
        for row in rows:
            expires_at = _row_value(row, "expires_at")
            results.append(
                {
                    "role_name": str(_row_value(row, "role_name")),
                    "managed_execution_id": str(_row_value(row, "managed_execution_id")),
                    "owner_kind": str(_row_value(row, "owner_kind")),
                    "agent_run_id": (
                        str(value)
                        if (value := _row_value(row, "agent_run_id")) is not None
                        else None
                    ),
                    "session_id": str(_row_value(row, "session_id")),
                    "project_id": str(_row_value(row, "project_id")),
                    "expires_at": (
                        expires_at.isoformat()
                        if isinstance(expires_at, datetime)
                        else str(expires_at)
                    ),
                    "login_capable": bool(_row_value(row, "login_capable")),
                    "active_sessions": int(_row_value(row, "active_sessions")),
                }
            )
        return results

    def reconcile(self) -> int:
        self.heartbeat()
        deadline = time.monotonic() + REVOCATION_DRAIN_TIMEOUT_SECONDS
        while True:
            row = self._database.fetchone(
                f"SELECT {self.auth_schema}.reconcile_daemon(%s)",
                (self._machine_id,),
            )
            if row is None:
                raise RuntimeError("managed credential reconciliation returned no result")
            reconciled_count = int(_row_value(row, "reconcile_daemon"))
            if reconciled_count >= 0 or time.monotonic() >= deadline:
                break
            time.sleep(REVOCATION_POLL_SECONDS)
        if reconciled_count < 0:
            reconciled_count = 0
        if not self._runtime_root.exists():
            return reconciled_count

        for execution_root in self._runtime_root.iterdir():
            if not execution_root.is_dir():
                continue
            try:
                execution_id = UUID(execution_root.name)
            except ValueError:
                continue
            retry = self._load_retry_record(execution_root)
            if retry is not None:
                self.revoke(
                    execution_id,
                    generation=retry[0],
                    reason=retry[1],
                )
            active = self._database.fetchone(
                f"SELECT {self.auth_schema}.managed_execution_is_login_capable(%s)",
                (execution_id,),
            )
            if active is not None and not bool(
                _row_value(active, "managed_execution_is_login_capable")
            ):
                self._delete_bootstrap(execution_id, None)
        return reconciled_count

    @staticmethod
    def _validate_expiry(issued_at: datetime, expires_at: datetime) -> datetime:
        if expires_at.tzinfo is None:
            raise ValueError("managed credential expiry must be timezone-aware")
        normalized = expires_at.astimezone(UTC)
        if normalized <= issued_at:
            raise ValueError("managed credential expiry must be in the future")
        if normalized > issued_at + MAX_ROLE_LIFETIME:
            raise ValueError(
                f"managed credential lifetime exceeds {MAX_ROLE_LIFETIME.total_seconds() / 3600:g} hours"
            )
        return normalized

    def _scoped_dsn(self, role_name: str, password: str, execution_id: UUID) -> str:
        parsed = conninfo_to_dict(self._database.conninfo)
        parsed.update(
            user=role_name,
            password=password,
            application_name=f"gobby-agent-{execution_id}",
        )
        return make_conninfo("", **parsed)

    def _materialize_bootstrap(
        self,
        *,
        managed_execution_id: UUID,
        role_name: str,
        generation: int,
        expires_at: datetime,
        scoped_dsn: str,
    ) -> Path:
        execution_root = self._execution_root(managed_execution_id)
        self._ensure_private_directory(execution_root)
        bootstrap_path = execution_root / "bootstrap.json"
        payload = {
            "database_url": scoped_dsn,
            "credential_generation": generation,
            "expires_at": expires_at.isoformat(),
            "managed_execution_id": str(managed_execution_id),
            "role_name": role_name,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".bootstrap-",
            suffix=".json",
            dir=execution_root,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, bootstrap_path)
            os.chmod(bootstrap_path, 0o600)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            scoped_dsn = ""
        return bootstrap_path

    def _execution_root(self, managed_execution_id: UUID) -> Path:
        return self._runtime_root / str(managed_execution_id)

    def _ensure_private_directory(self, execution_root: Path) -> None:
        self._runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._runtime_root, 0o700)
        execution_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(execution_root, 0o700)

    def _delete_bootstrap(
        self,
        managed_execution_id: UUID,
        generation: int | None,
    ) -> None:
        bootstrap_path = self._execution_root(managed_execution_id) / "bootstrap.json"
        if generation is None:
            bootstrap_path.unlink(missing_ok=True)
            return
        try:
            payload = json.loads(bootstrap_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            bootstrap_path.unlink(missing_ok=True)
            return
        if payload.get("credential_generation") == generation:
            bootstrap_path.unlink(missing_ok=True)

    def _delete_retry_record(self, managed_execution_id: UUID) -> None:
        (self._execution_root(managed_execution_id) / "revocation-retry.json").unlink(
            missing_ok=True
        )

    def _write_retry_record(
        self,
        managed_execution_id: UUID,
        generation: int | None,
        reason: str,
        *,
        failure_code: str,
    ) -> None:
        execution_root = self._execution_root(managed_execution_id)
        self._ensure_private_directory(execution_root)
        retry_path = execution_root / "revocation-retry.json"
        payload = {
            "credential_generation": generation,
            "failure_code": failure_code,
            "managed_execution_id": str(managed_execution_id),
            "reason": reason,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        descriptor = os.open(retry_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(retry_path, 0o600)

    @staticmethod
    def _load_retry_record(execution_root: Path) -> tuple[int | None, str] | None:
        retry_path = execution_root / "revocation-retry.json"
        try:
            payload = json.loads(retry_path.read_text())
            generation_value = payload.get("credential_generation")
            generation = int(generation_value) if generation_value is not None else None
            reason = str(payload["reason"])
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return None
        return generation, reason
