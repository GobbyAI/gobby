"""Lightweight helpers for the install-scoped daemon API token."""

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from gobby.paths import get_gobby_home

# This is a filename, not a credential value.
LOCAL_API_TOKEN_FILENAME = "local_cli_token"  # nosec B105
GOBBY_AGENT_API_TOKEN_ENV = "GOBBY_AGENT_API_TOKEN"
_AGENT_TOKEN_VERSION = "gobby-agent-v1"

# Expiry is defense-in-depth: the daemon's per-request run-liveness check is
# the real revocation, so the untimed ceiling never strands a legitimate long
# run mid-flight, and resume re-mints a fresh capability.
AGENT_TOKEN_MAX_TTL_SECONDS = 86400
_AGENT_TOKEN_TIMEOUT_GRACE_SECONDS = 60


@dataclass(frozen=True)
class AgentApiTokenClaims:
    """Identity bound into a spawned agent's daemon capability."""

    agent_run_id: str
    session_id: str
    project_id: str
    iat: int
    exp: int


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
) -> str:
    """Mint a signed daemon capability bound to one managed agent identity.

    A run-declared timeout bounds the token to that timeout plus a fixed
    grace; untimed runs get the fixed ``AGENT_TOKEN_MAX_TTL_SECONDS`` ceiling.
    """
    iat = int(time.time())
    if timeout_seconds is not None:
        exp = iat + int(timeout_seconds) + _AGENT_TOKEN_TIMEOUT_GRACE_SECONDS
    else:
        exp = iat + AGENT_TOKEN_MAX_TTL_SECONDS
    payload = json.dumps(
        {
            "agent_run_id": agent_run_id,
            "exp": exp,
            "iat": iat,
            "project_id": project_id,
            "session_id": session_id,
        },
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
    """Verify and decode a run-bound agent capability.

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
    session_id = raw.get("session_id")
    project_id = raw.get("project_id")
    if not all(
        isinstance(value, str) and value for value in (agent_run_id, session_id, project_id)
    ):
        return None
    iat = raw.get("iat")
    exp = raw.get("exp")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (iat, exp)):
        return None
    assert isinstance(agent_run_id, str)
    assert isinstance(session_id, str)
    assert isinstance(project_id, str)
    assert isinstance(iat, int)
    assert isinstance(exp, int)
    if time.time() >= exp:
        return None
    return AgentApiTokenClaims(
        agent_run_id=agent_run_id,
        session_id=session_id,
        project_id=project_id,
        iat=iat,
        exp=exp,
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
