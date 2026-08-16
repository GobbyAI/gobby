"""Lightweight helpers for the install-scoped daemon API token."""

import base64
import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from gobby.paths import get_gobby_home
from gobby.utils.machine_id import get_machine_id

# This is a filename, not a credential value.
LOCAL_API_TOKEN_FILENAME = "local_cli_token"  # nosec B105
GOBBY_AGENT_API_TOKEN_ENV = "GOBBY_AGENT_API_TOKEN"
GOBBY_MANAGED_EXECUTION_ID_ENV = "GOBBY_MANAGED_EXECUTION_ID"
_AGENT_TOKEN_VERSION = "gobby-agent-v1"

# Expiry is defense-in-depth: the daemon's per-request owner-liveness check is
# authoritative, while the untimed ceiling avoids stranding legitimate long
# agent runs and resume re-mints a fresh capability.
AGENT_TOKEN_MAX_TTL_SECONDS = 86400
_AGENT_TOKEN_TIMEOUT_GRACE_SECONDS = 60


@dataclass(frozen=True)
class AgentApiTokenClaims:
    """Identity bound into a managed execution's daemon capability."""

    session_id: str
    project_id: str
    machine_id: str
    iat: int
    exp: int
    agent_run_id: str | None = None
    managed_execution_id: str | None = None
    kind: str | None = None


def local_token_path() -> Path:
    """Return the local daemon API token path."""
    return get_gobby_home() / LOCAL_API_TOKEN_FILENAME


def read_local_api_token() -> str | None:
    """Read the local daemon API token when a non-empty file exists."""
    try:
        token = local_token_path().read_text().strip()
    except FileNotFoundError:
        return None
    return token or None


def issue_agent_api_token(
    operator_token: str,
    *,
    agent_run_id: str,
    session_id: str,
    project_id: str,
    timeout_seconds: float | None = None,
    machine_id: str | None = None,
) -> str:
    """Mint a signed daemon capability bound to one managed agent identity.

    A run-declared timeout bounds the token to that timeout plus a fixed
    grace; untimed runs get the fixed ``AGENT_TOKEN_MAX_TTL_SECONDS`` ceiling.
    """
    if timeout_seconds is not None:
        ttl_seconds = math.ceil(timeout_seconds) + _AGENT_TOKEN_TIMEOUT_GRACE_SECONDS
    else:
        ttl_seconds = AGENT_TOKEN_MAX_TTL_SECONDS
    return _issue_managed_api_token(
        operator_token,
        owner_claim="agent_run_id",
        owner_id=agent_run_id,
        session_id=session_id,
        project_id=project_id,
        machine_id=machine_id,
        ttl_seconds=ttl_seconds,
        kind="agent_run",
    )


def issue_tool_api_token(
    operator_token: str,
    *,
    managed_execution_id: str,
    session_id: str,
    project_id: str,
    timeout_seconds: float,
    machine_id: str | None = None,
) -> str:
    """Mint a daemon capability bounded to one managed tool request."""
    return _issue_managed_api_token(
        operator_token,
        owner_claim="managed_execution_id",
        owner_id=managed_execution_id,
        session_id=session_id,
        project_id=project_id,
        machine_id=machine_id,
        ttl_seconds=max(1, math.ceil(timeout_seconds)),
        kind="tool_chat",
    )


def issue_maintenance_api_token(
    operator_token: str,
    *,
    execution_id: str,
    project_id: str,
    timeout_seconds: float,
    machine_id: str | None = None,
) -> str:
    """Mint a daemon capability bound to one maintenance execution."""
    return _issue_managed_api_token(
        operator_token,
        owner_claim="managed_execution_id",
        owner_id=execution_id,
        session_id=execution_id,
        project_id=project_id,
        machine_id=machine_id,
        ttl_seconds=max(1, math.ceil(timeout_seconds)),
        kind="maintenance",
    )


def _issue_managed_api_token(
    operator_token: str,
    *,
    owner_claim: str,
    owner_id: str,
    session_id: str,
    project_id: str,
    ttl_seconds: int,
    machine_id: str | None = None,
    kind: str | None = None,
) -> str:
    resolved_machine = machine_id or get_machine_id()
    if not resolved_machine:
        raise ValueError("capability tokens require a machine_id")
    iat = int(time.time())
    exp = iat + ttl_seconds
    claims: dict[str, object] = {
        "exp": exp,
        "iat": iat,
        "machine_id": resolved_machine,
        owner_claim: owner_id,
        "project_id": project_id,
        "session_id": session_id,
    }
    if kind is not None:
        claims["kind"] = kind
    payload = json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded_payload = _urlsafe_encode(payload)
    signed = f"{_AGENT_TOKEN_VERSION}.{encoded_payload}"
    signature = hmac.new(operator_token.encode(), signed.encode(), hashlib.sha256).digest()
    return f"{signed}.{_urlsafe_encode(signature)}"


def verify_agent_api_token(
    token: str,
    operator_token: str,
) -> AgentApiTokenClaims | None:
    """Verify and decode a single-owner managed execution capability.

    Tokens without integer ``iat``/``exp`` claims and tokens at or past
    their expiry are rejected outright.
    """
    try:
        version, encoded_payload, encoded_signature = token.split(".", maxsplit=2)
        if version != _AGENT_TOKEN_VERSION:
            return None
        signed = f"{version}.{encoded_payload}"
        expected = hmac.new(operator_token.encode(), signed.encode(), hashlib.sha256).digest()
        supplied = _urlsafe_decode(encoded_signature)
        if not hmac.compare_digest(expected, supplied):
            return None
        raw: object = json.loads(_urlsafe_decode(encoded_payload))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    agent_run_id = raw.get("agent_run_id")
    managed_execution_id = raw.get("managed_execution_id")
    session_id = raw.get("session_id")
    project_id = raw.get("project_id")
    machine_id = raw.get("machine_id")
    owner_claims = [raw[name] for name in ("agent_run_id", "managed_execution_id") if name in raw]
    if len(owner_claims) != 1:
        return None
    if not isinstance(owner_claims[0], str) or not owner_claims[0]:
        return None
    if not all(isinstance(value, str) and value for value in (session_id, project_id, machine_id)):
        return None
    iat = raw.get("iat")
    exp = raw.get("exp")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (iat, exp)):
        return None
    assert isinstance(session_id, str)
    assert isinstance(project_id, str)
    assert isinstance(machine_id, str)
    assert isinstance(iat, int)
    assert isinstance(exp, int)
    if time.time() >= exp:
        return None
    kind = raw.get("kind")
    if kind is not None and (not isinstance(kind, str) or not kind):
        return None
    return AgentApiTokenClaims(
        agent_run_id=agent_run_id if isinstance(agent_run_id, str) else None,
        managed_execution_id=(
            managed_execution_id if isinstance(managed_execution_id, str) else None
        ),
        session_id=session_id,
        project_id=project_id,
        machine_id=machine_id,
        iat=iat,
        exp=exp,
        kind=kind,
    )


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


# Spawn-env identity forwarded beside the run capability so context-bearing
# routes can bind the request to the token's claims.
_AGENT_IDENTITY_ENV_HEADERS = (
    ("GOBBY_SESSION_ID", "X-Gobby-Session-Id"),
    ("GOBBY_PROJECT_ID", "X-Gobby-Caller-Project-Id"),
    ("GOBBY_AGENT_RUN_ID", "X-Gobby-Agent-Run-Id"),
    (GOBBY_MANAGED_EXECUTION_ID_ENV, "X-Gobby-Managed-Execution-Id"),
)


def daemon_auth_headers() -> dict[str, str]:
    """Build daemon bearer headers, preferring a managed run capability."""
    agent_token = os.environ.get(GOBBY_AGENT_API_TOKEN_ENV, "").strip()
    if agent_token:
        headers = {"Authorization": f"Bearer {agent_token}"}
        for env_name, header_name in _AGENT_IDENTITY_ENV_HEADERS:
            value = os.environ.get(env_name, "").strip()
            if value:
                headers[header_name] = value
        return headers
    token = read_local_api_token()
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}
