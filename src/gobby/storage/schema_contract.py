"""Identity-enforcing process boundary for the gdaemon schema runner."""

from __future__ import annotations

import importlib.resources
import json
import logging
import os
import subprocess
from typing import cast

from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

EXPECTED_IDENTITY_ENV = "GOBBY_EXPECTED_SCHEMA_IDENTITY"
DATABASE_URL_ENV = "GOBBY_DATABASE_URL"
_IDENTITY_FILE = "schema_expected_identity.json"
_INTEGER_FIELDS = ("runner_protocol", "baseline_version", "latest_version")
_STRING_FIELDS = ("baseline_checksum", "latest_checksum", "assets_root_hash")


class SchemaContractError(RuntimeError):
    """Raised when Python cannot safely delegate schema authority to gdaemon."""


def expected_schema_identity() -> dict[str, int | str]:
    """Load and validate the release-pinned gdaemon schema identity."""
    raw = importlib.resources.files("gobby.storage").joinpath(_IDENTITY_FILE).read_text()
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise SchemaContractError(f"Packaged {_IDENTITY_FILE} must contain a JSON object")
    values = cast(dict[str, object], parsed)
    expected_fields = {*_INTEGER_FIELDS, *_STRING_FIELDS}
    if set(values) != expected_fields:
        raise SchemaContractError(
            f"Packaged {_IDENTITY_FILE} must contain exactly {sorted(expected_fields)}"
        )

    identity: dict[str, int | str] = {}
    for field in _INTEGER_FIELDS:
        value = values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaContractError(f"Packaged schema identity field {field} must be an integer")
        identity[field] = value
    for field in _STRING_FIELDS:
        value = values[field]
        if not isinstance(value, str) or not value:
            raise SchemaContractError(f"Packaged schema identity field {field} must be a string")
        identity[field] = value
    return identity


def expected_schema_identity_json() -> str:
    """Serialize the release-pinned identity in stable compact form."""
    return json.dumps(expected_schema_identity(), separators=(",", ":"), sort_keys=True)


def latest_schema_version() -> int:
    """Return the release-pinned latest schema version."""
    value = expected_schema_identity()["latest_version"]
    if not isinstance(value, int):
        raise SchemaContractError("Packaged latest schema version must be an integer")
    return value


def _run_gdaemon(database_url: str, args: list[str], *, action: str) -> None:
    """Run one identity-enforcing gdaemon schema action."""
    binary = resolve_native_bin("gdaemon")
    if binary is None:
        raise SchemaContractError(
            f"gdaemon is required to {action}; run `gobby install` to install it"
        )

    env = os.environ.copy()
    env[DATABASE_URL_ENV] = database_url
    env[EXPECTED_IDENTITY_ENV] = expected_schema_identity_json()
    try:
        result = subprocess.run(
            [binary, *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise SchemaContractError(
            f"gdaemon {action} timed out after {exc.timeout:g} seconds; "
            "check PostgreSQL availability and retry"
        ) from exc
    except OSError as exc:
        raise SchemaContractError(f"Failed to launch gdaemon: {exc}") from exc
    if result.returncode != 0:
        detail = (
            result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        )
        raise SchemaContractError(
            f"gdaemon {action} failed: {detail}. Run `gobby install` to refresh gdaemon"
        )


def sweep_test_schemas(database_url: str, *, age_hours: int) -> None:
    """Delegate abandoned test-schema sweeping to gdaemon."""
    _run_gdaemon(
        database_url,
        ["schema", "sweep-test-schemas", "--age-hours", str(age_hours)],
        action="schema sweep-test-schemas",
    )


def apply_schema(
    database_url: str,
    *,
    schema: str | None = None,
    destructive: bool = False,
) -> None:
    """Apply schema assets through one identity-enforcing gdaemon process."""
    args = ["schema", "apply"]
    if schema is not None:
        args.extend(["--schema", schema])
    if destructive:
        args.append("--destructive")
    _run_gdaemon(database_url, args, action="schema apply")
    logger.info("gdaemon schema apply completed for schema %s", schema or "connection default")


def verify_schema(database_url: str) -> None:
    """Verify binary identity and the live schema without applying changes."""
    _run_gdaemon(database_url, ["schema", "verify"], action="schema verify")
    logger.info("gdaemon schema verify completed")
