"""Sign test capabilities through the daemon-owned key file."""

from pathlib import Path

from gobby.utils.local_token import read_managed_signing_key


def daemon_signing_key(bootstrap: Path | None = None) -> bytes:
    key = read_managed_signing_key(bootstrap)
    assert key is not None, "isolated daemon signing key is unavailable"
    return key
