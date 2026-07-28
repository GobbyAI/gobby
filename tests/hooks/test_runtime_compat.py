"""Runtime compatibility diagnostics for the managed ghook binary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.hooks.runtime_compat import (
    MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA,
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    GhookRuntimeState,
    read_ghook_runtime_diagnostic,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


def _write_stamp(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_absent_runtime_stamp_is_explicit_and_non_degrading(tmp_path: Path) -> None:
    diagnostic = read_ghook_runtime_diagnostic(tmp_path / "missing.json")

    assert diagnostic.state is GhookRuntimeState.ABSENT
    assert diagnostic.is_degraded is False
    assert diagnostic.schema_version is None
    assert diagnostic.ghook_version is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"schema_version": True, "ghook_version": "0.7.1"},
        {"schema_version": 1, "ghook_version": 7},
        {"schema_version": 1, "ghook_version": "not-a-version"},
    ],
)
def test_malformed_runtime_stamp_has_typed_state(tmp_path: Path, payload: object) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    _write_stamp(stamp, payload)

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is GhookRuntimeState.MALFORMED
    assert diagnostic.is_degraded is True
    assert diagnostic.detail


def test_invalid_json_runtime_stamp_is_malformed(tmp_path: Path) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    stamp.write_text("{not-json")

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is GhookRuntimeState.MALFORMED
    assert "JSON" in diagnostic.detail


def test_non_utf8_runtime_stamp_is_malformed(tmp_path: Path) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    stamp.write_bytes(b"\xff\xfe")

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is GhookRuntimeState.MALFORMED
    assert "UTF-8" in diagnostic.detail


def test_schema_mismatch_takes_precedence_over_version_freshness(tmp_path: Path) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    _write_stamp(stamp, {"schema_version": 99, "ghook_version": "0.1.0"})

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is GhookRuntimeState.SCHEMA_MISMATCH
    assert diagnostic.schema_version == 99
    assert diagnostic.ghook_version == "0.1.0"
    assert diagnostic.is_degraded is True


def test_stale_runtime_version_is_typed(tmp_path: Path) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    _write_stamp(
        stamp,
        {
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "ghook_version": "0.1.0",
        },
    )

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is GhookRuntimeState.STALE_VERSION
    assert diagnostic.is_degraded is True
    assert MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA in diagnostic.detail


@pytest.mark.parametrize(
    ("version", "expected_state"),
    [
        ("0.7.2", GhookRuntimeState.STALE_VERSION),
        ("0.7.3", GhookRuntimeState.COMPATIBLE),
    ],
)
def test_agent_identity_runtime_version_floor(
    tmp_path: Path,
    version: str,
    expected_state: GhookRuntimeState,
) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    _write_stamp(
        stamp,
        {
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "ghook_version": version,
        },
    )

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is expected_state


def test_current_runtime_stamp_is_compatible(tmp_path: Path) -> None:
    stamp = tmp_path / ".ghook-runtime.json"
    _write_stamp(
        stamp,
        {
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "ghook_version": MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA,
        },
    )

    diagnostic = read_ghook_runtime_diagnostic(stamp)

    assert diagnostic.state is GhookRuntimeState.COMPATIBLE
    assert diagnostic.is_degraded is False
    assert diagnostic.to_dict()["compatible"] is True


def test_supported_schema_uses_managed_ghook_version_floor() -> None:
    assert MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA == MANAGED_BIN_VERSION_PINS["ghook"]
