"""Typed compatibility diagnostics for ghook's runtime stamp."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from gobby.cli.utils import get_gobby_home
from gobby.install.bin_freshness_models import is_at_least_version, parse_version_tuple
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION = 1
SUPPORTED_HOOK_RESPONSE_CAPABILITY = "hook-response.v1"
MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA = MANAGED_BIN_VERSION_PINS["ghook"]
GHOOK_RUNTIME_STAMP_RELATIVE_PATH = Path("bin") / ".ghook-runtime.json"


class GhookRuntimeState(str, Enum):
    """Compatibility states exposed by daemon health and status surfaces."""

    ABSENT = "absent"
    COMPATIBLE = "compatible"
    MALFORMED = "malformed"
    SCHEMA_MISMATCH = "schema_mismatch"
    STALE_VERSION = "stale_version"


@dataclass(frozen=True, slots=True)
class GhookRuntimeDiagnostic:
    """Parsed ghook runtime stamp and its compatibility decision."""

    state: GhookRuntimeState
    stamp_path: str
    detail: str
    expected_schema_version: int = SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION
    minimum_ghook_version: str = MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA
    schema_version: int | None = None
    ghook_version: str | None = None
    response_capability: str | None = None

    @property
    def is_degraded(self) -> bool:
        """Return whether this state should degrade health diagnostics."""
        return self.state not in {GhookRuntimeState.ABSENT, GhookRuntimeState.COMPATIBLE}

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible health payload."""
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["compatible"] = (
            True
            if self.state is GhookRuntimeState.COMPATIBLE
            else None
            if self.state is GhookRuntimeState.ABSENT
            else False
        )
        return payload


def ghook_runtime_stamp_path(home: Path | None = None) -> Path:
    """Return the managed ghook runtime-stamp path."""
    return (home or get_gobby_home()) / GHOOK_RUNTIME_STAMP_RELATIVE_PATH


def envelope_has_hook_response_capability(value: object) -> bool:
    """True when the request-carried producer advertised the supported floor."""
    return value == SUPPORTED_HOOK_RESPONSE_CAPABILITY


def _diagnostic(
    state: GhookRuntimeState,
    path: Path,
    detail: str,
    *,
    schema_version: int | None = None,
    ghook_version: str | None = None,
    response_capability: str | None = None,
) -> GhookRuntimeDiagnostic:
    return GhookRuntimeDiagnostic(
        state=state,
        stamp_path=str(path),
        detail=detail,
        schema_version=schema_version,
        ghook_version=ghook_version,
        response_capability=response_capability,
    )


def read_ghook_runtime_diagnostic(
    stamp_path: Path | None = None,
) -> GhookRuntimeDiagnostic:
    """Read and classify the ghook runtime stamp without raising."""
    path = stamp_path or ghook_runtime_stamp_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _diagnostic(
            GhookRuntimeState.ABSENT,
            path,
            "Runtime stamp is absent; compatibility has not been observed.",
        )
    except UnicodeDecodeError:
        return _diagnostic(
            GhookRuntimeState.MALFORMED,
            path,
            "Runtime stamp is not valid UTF-8 JSON.",
        )
    except OSError as exc:
        return _diagnostic(
            GhookRuntimeState.MALFORMED,
            path,
            f"Runtime stamp could not be read ({type(exc).__name__}).",
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _diagnostic(
            GhookRuntimeState.MALFORMED,
            path,
            "Runtime stamp is not valid UTF-8 JSON.",
        )

    if not isinstance(payload, dict):
        return _diagnostic(
            GhookRuntimeState.MALFORMED,
            path,
            "Runtime stamp must contain a JSON object.",
        )

    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version <= 0:
        return _diagnostic(
            GhookRuntimeState.MALFORMED,
            path,
            "Runtime stamp schema_version must be a positive integer.",
        )

    ghook_version = payload.get("ghook_version")
    if not isinstance(ghook_version, str) or parse_version_tuple(ghook_version) is None:
        return _diagnostic(
            GhookRuntimeState.MALFORMED,
            path,
            "Runtime stamp ghook_version must be a semantic version string.",
            schema_version=schema_version,
        )
    ghook_version = ghook_version.strip()

    raw_capability = payload.get("response_capability")
    response_capability = (
        raw_capability if isinstance(raw_capability, str) and raw_capability else None
    )

    if schema_version != SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION:
        return _diagnostic(
            GhookRuntimeState.SCHEMA_MISMATCH,
            path,
            (
                f"ghook envelope schema {schema_version} does not match daemon schema "
                f"{SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION}."
            ),
            schema_version=schema_version,
            ghook_version=ghook_version,
            response_capability=response_capability,
        )

    if not is_at_least_version(
        ghook_version,
        MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA,
    ):
        return _diagnostic(
            GhookRuntimeState.STALE_VERSION,
            path,
            (
                f"ghook {ghook_version} is below the managed minimum "
                f"{MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA} for envelope schema "
                f"{SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION}."
            ),
            schema_version=schema_version,
            ghook_version=ghook_version,
            response_capability=response_capability,
        )

    return _diagnostic(
        GhookRuntimeState.COMPATIBLE,
        path,
        "ghook runtime stamp matches the daemon envelope schema and version policy.",
        schema_version=schema_version,
        ghook_version=ghook_version,
        response_capability=response_capability,
    )


__all__ = [
    "GHOOK_RUNTIME_STAMP_RELATIVE_PATH",
    "MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA",
    "SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION",
    "SUPPORTED_HOOK_RESPONSE_CAPABILITY",
    "GhookRuntimeDiagnostic",
    "GhookRuntimeState",
    "envelope_has_hook_response_capability",
    "ghook_runtime_stamp_path",
    "read_ghook_runtime_diagnostic",
]
