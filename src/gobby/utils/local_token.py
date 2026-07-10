"""Lightweight helpers for the install-scoped daemon API token."""

from pathlib import Path

from gobby.config.bootstrap_io import default_gobby_home

LOCAL_API_TOKEN_FILENAME = "local_cli_token"


def local_token_path() -> Path:
    """Return the local daemon API token path."""
    return default_gobby_home() / LOCAL_API_TOKEN_FILENAME


def read_local_api_token() -> str | None:
    """Read the local daemon API token when a non-empty file exists."""
    try:
        token = local_token_path().read_text().strip()
    except FileNotFoundError:
        return None
    return token or None


def daemon_auth_headers() -> dict[str, str]:
    """Build daemon bearer headers from the local token file."""
    token = read_local_api_token()
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}
