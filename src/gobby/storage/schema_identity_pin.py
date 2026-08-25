"""Shared gdaemon schema identity probe and pin serialisation.

``gobby cutover`` and ``scripts/generate_schema_expected_identity.py`` both query
``gdaemon schema version --json`` and write the result as the packaged
``schema_expected_identity.json`` that CI ``cmp``s. This module is the single
definition of the field contract, its validation, and the byte formats.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

INTEGER_IDENTITY_FIELDS = ("runner_protocol", "baseline_version", "latest_version")
STRING_IDENTITY_FIELDS = ("baseline_checksum", "latest_checksum", "assets_root_hash")
IDENTITY_FIELDS = frozenset((*INTEGER_IDENTITY_FIELDS, *STRING_IDENTITY_FIELDS))
_PROBE_TIMEOUT_SECONDS = 30


class SchemaIdentityError(RuntimeError):
    """Raised when gdaemon cannot provide the exact schema identity contract."""


def validate_identity(parsed: object) -> dict[str, int | str]:
    """Narrow a parsed ``schema version --json`` payload to the identity contract."""
    if not isinstance(parsed, dict):
        raise SchemaIdentityError("gdaemon schema identity must be a JSON object")
    values = cast(dict[str, object], parsed)
    if set(values) != IDENTITY_FIELDS:
        raise SchemaIdentityError(
            f"gdaemon schema identity must contain exactly {sorted(IDENTITY_FIELDS)}"
        )

    identity: dict[str, int | str] = {}
    for field in INTEGER_IDENTITY_FIELDS:
        value = values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaIdentityError(f"gdaemon schema identity field {field} must be an integer")
        identity[field] = value
    for field in STRING_IDENTITY_FIELDS:
        value = values[field]
        if not isinstance(value, str) or not value:
            raise SchemaIdentityError(f"gdaemon schema identity field {field} must be a string")
        identity[field] = value
    return identity


def probe_identity(gdaemon: Path, *, cwd: Path | None = None) -> dict[str, int | str]:
    """Run ``gdaemon schema version --json`` and return its validated identity."""
    try:
        result = subprocess.run(  # nosec B603 - operator-supplied executable, fixed arguments
            [str(gdaemon), "schema", "version", "--json"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SchemaIdentityError(
            f"gdaemon schema version timed out after {_PROBE_TIMEOUT_SECONDS} seconds"
        ) from exc
    except OSError as exc:
        raise SchemaIdentityError(f"failed to launch gdaemon: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SchemaIdentityError(f"gdaemon schema version failed: {detail}")
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SchemaIdentityError("gdaemon schema version returned invalid JSON") from exc
    return validate_identity(parsed)


def pin_bytes(identity: dict[str, int | str]) -> bytes:
    """Serialise an identity as the packaged ``schema_expected_identity.json`` bytes."""
    return (json.dumps(identity, indent=2, sort_keys=True) + "\n").encode()


def stamp_bytes(identity: dict[str, int | str]) -> bytes:
    """Serialise an identity as the installer's ``.gdaemon-schema-identity.json`` bytes."""
    return (json.dumps(identity, separators=(",", ":"), sort_keys=True) + "\n").encode()


__all__ = [
    "IDENTITY_FIELDS",
    "INTEGER_IDENTITY_FIELDS",
    "STRING_IDENTITY_FIELDS",
    "SchemaIdentityError",
    "pin_bytes",
    "probe_identity",
    "stamp_bytes",
    "validate_identity",
]
