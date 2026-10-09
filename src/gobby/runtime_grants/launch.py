"""Materialize a managed child grant file and launch envelope."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gobby.runtime_grants.schema import GrantBundle, GrantDeployment, PostgresDirect
from gobby.runtime_grants.signing import sign_grant
from gobby.utils.local_token import (
    issue_agent_api_token,
    issue_maintenance_api_token,
    issue_tool_api_token,
)


@dataclass(frozen=True)
class ManagedLaunch:
    grant_path: Path
    env: dict[str, str]


def write_grant_file(
    path: Path, grant: GrantBundle, *, managed_api_token: str | None = None
) -> Path:
    """Atomically write a mode-0600 grant file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".grant-",
        suffix=".json",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    stream = None
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb")
        payload = json.loads(grant.model_dump_canonical())
        if managed_api_token is None:
            managed_api_token = grant.managed_api_token
        if managed_api_token is not None:
            payload["managed_api_token"] = managed_api_token
        stream.write((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        os.replace(temporary_path, path)
    except Exception:
        if stream is not None:
            stream.close()
        else:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def rewrite_managed_grant_file(
    path: Path,
    *,
    managed_execution_id: str,
    scoped_dsn: str,
    role_name: str,
    credential_generation: int,
    issued_at: datetime,
    expires_at: datetime,
    deployment_token: str,
    fencing_epoch: int,
    signing_secret: str,
) -> Path:
    """Atomically replace a launch grant with a signed successor credential."""
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("managed capability envelope is not private")
        grant = GrantBundle.model_validate_json(stream.read())
    if grant.principal.execution_id != managed_execution_id:
        raise ValueError("managed launch grant execution identity does not match credential")
    expires_at_epoch = int(expires_at.timestamp())
    postgres = PostgresDirect(
        dsn=scoped_dsn,
        role_name=role_name,
        credential_generation=credential_generation,
        valid_until=expires_at_epoch,
    )
    unsigned = grant.model_copy(
        update={
            "deployment": GrantDeployment(
                token=deployment_token,
                fencing_epoch=fencing_epoch,
            ),
            "capabilities": grant.capabilities.model_copy(update={"postgres": postgres}),
            "issued_at": int(issued_at.timestamp()),
            "expires_at": expires_at_epoch,
        }
    )
    from gobby.utils.local_token import AGENT_TOKEN_MAX_TTL_SECONDS, read_managed_signing_key

    token = None
    if grant.managed_api_token is not None:
        key = read_managed_signing_key()
        if key is None:
            raise RuntimeError("managed capability renewal signing key is unavailable")
        token = _issue_grant_capability(unsigned, key, AGENT_TOKEN_MAX_TTL_SECONDS)
    return write_grant_file(path, sign_grant(unsigned, signing_secret), managed_api_token=token)


def materialize_managed_launch(
    grant: GrantBundle,
    *,
    dest_dir: Path,
    signing_key: bytes,
    deadline_seconds: float,
) -> ManagedLaunch:
    """Write the grant file and mint a matching run-scoped capability token."""
    token = _issue_grant_capability(grant, signing_key, deadline_seconds)
    grant_path = write_grant_file(dest_dir / "grant.json", grant, managed_api_token=token)
    env = {
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(grant_path),
        "GOBBY_AGENT_API_TOKEN": token,
        "GOBBY_PROJECT_ID": grant.principal.project_id,
    }
    owner_key = (
        "GOBBY_AGENT_RUN_ID"
        if grant.principal.kind == "agent_run"
        else "GOBBY_MANAGED_EXECUTION_ID"
    )
    # The native token reader binds the private envelope to this child identity.
    # Maintenance has an execution owner but no session or checkout.
    if grant.principal.execution_id is not None:
        env[owner_key] = grant.principal.execution_id
    if grant.principal.session_id is not None:
        env["GOBBY_SESSION_ID"] = grant.principal.session_id
    return ManagedLaunch(
        grant_path=grant_path,
        env=env,
    )


def _issue_grant_capability(grant: GrantBundle, signing_key: bytes, deadline_seconds: float) -> str:
    ttl = max(1, int(deadline_seconds))
    if grant.principal.kind == "tool_chat":
        if grant.principal.execution_id is None or grant.principal.session_id is None:
            raise ValueError("tool_chat grant is missing execution or session identity")
        token = issue_tool_api_token(
            signing_key,
            managed_execution_id=grant.principal.execution_id,
            session_id=grant.principal.session_id,
            project_id=grant.principal.project_id,
            machine_id=grant.principal.machine_id,
            timeout_seconds=ttl,
        )
    elif grant.principal.kind == "maintenance":
        if grant.principal.execution_id is None:
            raise ValueError("maintenance grant is missing execution identity")
        token = issue_maintenance_api_token(
            signing_key,
            execution_id=grant.principal.execution_id,
            project_id=grant.principal.project_id,
            machine_id=grant.principal.machine_id,
            timeout_seconds=ttl,
        )
    else:
        if grant.principal.execution_id is None or grant.principal.session_id is None:
            raise ValueError("agent_run grant is missing execution or session identity")
        token = issue_agent_api_token(
            signing_key,
            agent_run_id=grant.principal.execution_id,
            session_id=grant.principal.session_id,
            project_id=grant.principal.project_id,
            machine_id=grant.principal.machine_id,
            timeout_seconds=ttl,
        )
    return token


_CHILD_ENV_BASE_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
)
_CHILD_ENV_OVERLAY_KEYS = (
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    "GOBBY_AGENT_API_TOKEN",
    "GOBBY_PROJECT_ID",
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_SESSION_ID",
)


def merge_child_env(extra: dict[str, str] | None) -> dict[str, str] | None:
    """Return an isolated subprocess env for a managed child."""
    if extra is None:
        return None
    env = {key: value for key in _CHILD_ENV_BASE_KEYS if (value := os.environ.get(key))}
    for key in _CHILD_ENV_OVERLAY_KEYS:
        value = extra.get(key)
        if value is not None:
            env[key] = value
    return env
