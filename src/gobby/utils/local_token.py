"""Lightweight helpers for the install-scoped daemon API token."""

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path

from gobby.paths import get_gobby_home

# This is a filename, not a credential value.
LOCAL_API_TOKEN_FILENAME = "local_cli_token"  # nosec B105
GOBBY_AGENT_API_TOKEN_ENV = "GOBBY_AGENT_API_TOKEN"
_AGENT_TOKEN_VERSION = "gobby-agent-v1"


@dataclass(frozen=True)
class AgentApiTokenClaims:
    """Identity bound into a spawned agent's daemon capability."""

    agent_run_id: str
    session_id: str
    project_id: str


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
) -> str:
    """Mint a signed daemon capability bound to one managed agent identity."""
    payload = json.dumps(
        {
            "agent_run_id": agent_run_id,
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
    """Verify and decode a run-bound agent capability."""
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
    assert isinstance(agent_run_id, str)
    assert isinstance(session_id, str)
    assert isinstance(project_id, str)
    return AgentApiTokenClaims(
        agent_run_id=agent_run_id,
        session_id=session_id,
        project_id=project_id,
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
