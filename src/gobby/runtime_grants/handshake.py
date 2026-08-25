"""Issue and challenge helpers for the runtime handshake."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal, PostgresDirect
from gobby.runtime_grants.service import GrantService
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    _urlsafe_encode,
    managed_token_signing_payload,
    verify_agent_api_token,
)

GRANT_TTL_SECONDS = 3600
_AGENT_TOKEN_VERSION = "gobby-agent-v1"


class HandshakeRejection(Exception):
    """Typed handshake failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def encode_grant_header(grant: GrantBundle) -> str:
    """Return the base64url header value for a signed grant."""
    return base64.urlsafe_b64encode(grant.model_dump_canonical()).decode().rstrip("=")


def _normalized_overlay_claim(project_id: str, code_overlay_project_id: str | None) -> str | None:
    """Validate an optional code-index overlay claim against its project."""
    if code_overlay_project_id is None:
        return None
    try:
        overlay = str(UUID(code_overlay_project_id))
    except ValueError:
        raise HandshakeRejection(
            "code_overlay_project_id must be a UUID", code="claims_mismatch"
        ) from None
    if overlay == project_id:
        raise HandshakeRejection(
            "code_overlay_project_id must differ from project_id",
            code="claims_mismatch",
        )
    return overlay


def decode_grant_header(value: str) -> GrantBundle:
    """Parse a presented ``X-Gobby-Runtime-Grant`` header."""
    padded = value + "=" * (-len(value) % 4)
    return GrantBundle.model_validate_json(base64.urlsafe_b64decode(padded))


def challenge_proof(
    nonce: bytes,
    *,
    kind: str,
    operator_token: str,
    claims: AgentApiTokenClaims | None = None,
) -> str:
    """HMAC the client nonce with the caller's credential secret."""
    if kind == "interactive":
        secret = operator_token.encode()
    elif kind == "managed":
        if claims is None:
            raise HandshakeRejection("managed challenge requires claims", code="claims_mismatch")
        secret = _recompute_capability_signature(operator_token, claims)
    else:
        raise HandshakeRejection(f"unknown challenge kind {kind}", code="claims_mismatch")
    return hmac.new(secret, nonce, hashlib.sha256).hexdigest()


def _recompute_capability_signature(operator_token: str, claims: AgentApiTokenClaims) -> bytes:
    payload = managed_token_signing_payload(
        {
            "exp": claims.exp,
            "iat": claims.iat,
            "machine_id": claims.machine_id,
            "project_id": claims.project_id,
            "session_id": claims.session_id,
            "agent_run_id": claims.agent_run_id,
            "managed_execution_id": claims.managed_execution_id,
            "kind": claims.kind,
        }
    )
    encoded = _urlsafe_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{_AGENT_TOKEN_VERSION}.{encoded}"
    return hmac.new(operator_token.encode(), signed.encode(), hashlib.sha256).digest()


@dataclass
class HandshakeService:
    grants: GrantService
    local_machine_id: str
    operator_token: str
    issue_postgres: Callable[[GrantPrincipal], PostgresDirect]
    admitted_projects: frozenset[str] | Callable[[str], bool]
    clock: Callable[[], int]
    principal_lock_key: Callable[[str, str, str], tuple[str, ...]] | None = None
    _locks: dict[tuple[str, ...], threading.Lock] = field(default_factory=dict)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def issue_for_operator(
        self,
        *,
        machine_id: str,
        project_id: str,
        session_id: str,
        code_overlay_project_id: str | None = None,
    ) -> GrantBundle:
        if machine_id != self.local_machine_id:
            raise HandshakeRejection(
                "operator machine_id must match the daemon machine",
                code="claims_mismatch",
            )
        if not self._project_admitted(project_id):
            raise HandshakeRejection("project is not admitted", code="claims_mismatch")
        code_overlay_project_id = _normalized_overlay_claim(project_id, code_overlay_project_id)
        principal = GrantPrincipal(
            kind="interactive",
            machine_id=machine_id,
            project_id=project_id,
            execution_id=None,
            session_id=session_id,
            code_overlay_project_id=code_overlay_project_id,
        )
        return self._issue(principal)

    def issue_for_agent(
        self,
        claims: AgentApiTokenClaims | None,
        *,
        machine_id: str,
        project_id: str,
    ) -> GrantBundle:
        if claims is None:
            raise HandshakeRejection("capability token is required", code="claims_mismatch")
        if claims.machine_id != machine_id or claims.project_id != project_id:
            raise HandshakeRejection(
                "body machine_id/project_id must equal verified claims",
                code="claims_mismatch",
            )
        kind = claims.kind or ("agent_run" if claims.agent_run_id is not None else "tool_chat")
        if kind not in {"agent_run", "tool_chat", "maintenance"}:
            raise HandshakeRejection("capability token kind is not managed", code="claims_mismatch")
        principal = GrantPrincipal(
            kind=kind,
            machine_id=claims.machine_id,
            project_id=claims.project_id,
            execution_id=claims.agent_run_id or claims.managed_execution_id,
            session_id=None if kind == "maintenance" else claims.session_id,
        )
        return self._issue(principal, token_exp=claims.exp)

    def issue_for_maintenance(
        self,
        *,
        machine_id: str,
        project_id: str,
        execution_id: str,
        code_overlay_project_id: str | None = None,
    ) -> GrantBundle:
        if machine_id != self.local_machine_id:
            raise HandshakeRejection(
                "maintenance machine_id must match the daemon machine",
                code="claims_mismatch",
            )
        if not self._project_admitted(project_id):
            raise HandshakeRejection("project is not admitted", code="claims_mismatch")
        code_overlay_project_id = _normalized_overlay_claim(project_id, code_overlay_project_id)
        principal = GrantPrincipal(
            kind="maintenance",
            machine_id=machine_id,
            project_id=project_id,
            execution_id=execution_id,
            session_id=None,
            code_overlay_project_id=code_overlay_project_id,
        )
        return self._issue(principal)

    def authenticate_managed_refresh(
        self,
        token: str | None,
        principal: GrantPrincipal,
    ) -> AgentApiTokenClaims:
        if token is None or not token:
            raise HandshakeRejection(
                "managed refresh requires the launch envelope token",
                code="managed_source",
            )
        claims = verify_agent_api_token(token, self.operator_token)
        if claims is None:
            raise HandshakeRejection("envelope token is invalid", code="managed_source")
        owner = claims.agent_run_id or claims.managed_execution_id
        if (
            claims.machine_id != principal.machine_id
            or claims.project_id != principal.project_id
            or owner != principal.execution_id
        ):
            raise HandshakeRejection("envelope token principal mismatch", code="managed_source")
        return claims

    def _issue(self, principal: GrantPrincipal, *, token_exp: int | None = None) -> GrantBundle:
        key = (
            self.principal_lock_key or (lambda kind, machine, project: (kind, machine, project))
        )(
            principal.kind,
            principal.machine_id,
            principal.project_id,
        )
        lock = self._lock_for(key)
        with lock:
            postgres = self.issue_postgres(principal)
            now = self.clock()
            ttl = GRANT_TTL_SECONDS
            if token_exp is not None:
                ttl = min(ttl, max(1, token_exp - now))
            ttl = min(ttl, max(1, postgres.valid_until - now))
            return self.grants.issue(
                principal=principal, postgres=postgres, now=now, ttl_seconds=ttl
            )

    def _project_admitted(self, project_id: str) -> bool:
        admitted = self.admitted_projects
        if callable(admitted):
            return admitted(project_id)
        return project_id in admitted

    def _lock_for(self, key: tuple[str, ...]) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock
