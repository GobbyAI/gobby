"""Materialize a managed child grant file and launch envelope."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gobby.runtime_grants.schema import GrantBundle
from gobby.utils.local_token import (
    issue_agent_api_token,
    issue_maintenance_api_token,
    issue_tool_api_token,
)


@dataclass(frozen=True)
class ManagedLaunch:
    grant_path: Path
    env: dict[str, str]


def write_grant_file(path: Path, grant: GrantBundle) -> Path:
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
        stream.write(grant.model_dump_canonical())
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
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


def materialize_managed_launch(
    grant: GrantBundle,
    *,
    dest_dir: Path,
    operator_token: str,
    deadline_seconds: float,
) -> ManagedLaunch:
    """Write the grant file and mint a matching run-scoped capability token."""
    grant_path = write_grant_file(dest_dir / "grant.json", grant)
    ttl = max(1, int(deadline_seconds))
    if grant.principal.kind == "tool_chat":
        if grant.principal.execution_id is None or grant.principal.session_id is None:
            raise ValueError("tool_chat grant is missing execution or session identity")
        token = issue_tool_api_token(
            operator_token,
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
            operator_token,
            execution_id=grant.principal.execution_id,
            project_id=grant.principal.project_id,
            machine_id=grant.principal.machine_id,
            timeout_seconds=ttl,
        )
    else:
        if grant.principal.execution_id is None or grant.principal.session_id is None:
            raise ValueError("agent_run grant is missing execution or session identity")
        token = issue_agent_api_token(
            operator_token,
            agent_run_id=grant.principal.execution_id,
            session_id=grant.principal.session_id,
            project_id=grant.principal.project_id,
            machine_id=grant.principal.machine_id,
            timeout_seconds=ttl,
        )
    return ManagedLaunch(
        grant_path=grant_path,
        env={
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(grant_path),
            "GOBBY_AGENT_API_TOKEN": token,
        },
    )


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
