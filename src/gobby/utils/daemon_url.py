"""Resolve the daemon client dial URL from env and bootstrap config."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from gobby.config.bootstrap import DEFAULT_DAEMON_PORT, BootstrapConfig, load_bootstrap
from gobby.config.bootstrap_io import bootstrap_path as default_bootstrap_path

DEFAULT_DAEMON_DIAL_URL = f"http://127.0.0.1:{DEFAULT_DAEMON_PORT}"


class DaemonUrlError(ValueError):
    """Raised when a configured daemon URL is not dialable."""


def daemon_url(bootstrap_path: str | Path | None = None) -> str:
    """Return the daemon URL using the shared client-dial contract."""
    return resolve_daemon_url(bootstrap_path=bootstrap_path)


def resolve_daemon_url(
    bootstrap_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the daemon dial URL from env overrides and bootstrap.yaml.

    Resolution order mirrors the Rust daemon_url.rs contract, with the
    Python-only bootstrap daemon_url field inserted before bind_host/port.
    """
    environ = os.environ if env is None else env
    if url := _env_url_override(environ):
        return url
    if url := _env_port_override(environ):
        return url

    path = _resolve_bootstrap_path(bootstrap_path)
    if not path.exists():
        return DEFAULT_DAEMON_DIAL_URL

    bootstrap = load_bootstrap(str(path))
    if bootstrap.daemon_url is not None:
        return validate_daemon_url(bootstrap.daemon_url, source="bootstrap daemon_url")
    return endpoint_to_url(bootstrap)


def endpoint_to_url(bootstrap: BootstrapConfig) -> str:
    """Convert bootstrap bind_host and daemon_port into a dial URL."""
    return f"http://{normalize_dial_host(bootstrap.bind_host)}:{bootstrap.daemon_port}"


def normalize_dial_host(host: str) -> str:
    """Convert daemon bind hosts into client-dialable hosts."""
    stripped = host.strip()
    if stripped in {"", "0.0.0.0", "::", "::0", "[::]"}:
        return "127.0.0.1"
    if ":" in stripped and not stripped.startswith("["):
        return f"[{stripped}]"
    return stripped


def validate_daemon_url(url: str, *, source: str = "daemon URL") -> str:
    """Validate and normalize an explicit daemon URL value."""
    normalized = url.strip().rstrip("/")
    if not normalized:
        raise DaemonUrlError(f"{source} must not be empty")

    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DaemonUrlError(f"{source} must be an http or https URL")
    if parsed.query or parsed.fragment:
        raise DaemonUrlError(f"{source} must not include a query string or fragment")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise DaemonUrlError(f"{source} has an invalid port") from exc
    return normalized


def _env_url_override(env: Mapping[str, str]) -> str | None:
    value = env.get("GOBBY_DAEMON_URL")
    if value is None or not value.strip():
        return None
    return validate_daemon_url(value, source="GOBBY_DAEMON_URL")


def _env_port_override(env: Mapping[str, str]) -> str | None:
    for name in ("GOBBY_PORT", "GOBBY_DAEMON_PORT"):
        url = _port_to_url(env.get(name))
        if url is not None:
            return url
    return None


def _port_to_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        port = int(value.strip())
    except ValueError:
        return None
    if not 0 <= port <= 65535:
        return None
    return f"http://127.0.0.1:{port}"


def _resolve_bootstrap_path(path: str | Path | None) -> Path:
    if path is None:
        return default_bootstrap_path()

    bootstrap_path = Path(path).expanduser()
    if bootstrap_path.name == "bootstrap.yaml":
        return bootstrap_path

    candidate = bootstrap_path.parent / "bootstrap.yaml"
    return candidate if candidate.exists() else bootstrap_path
