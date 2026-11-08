"""The installed gdaemon and gcode must be the archived checkout's schema head and version."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime_boundary import EvidenceError, contained_file, require

IDENTITY_KEYS = (
    "assets_root_hash",
    "baseline_checksum",
    "baseline_version",
    "latest_checksum",
    "latest_version",
    "runner_protocol",
)
ARCHIVED_IDENTITY_RELATIVE = "sources/gobby/src/gobby/storage/schema_expected_identity.json"
ARCHIVED_GCODE_CRATE_RELATIVE = "sources/gobby/crates/gcode/Cargo.toml"
BINARY_DIR_RELATIVE = "gobby-home/bin"
VERSION_TIMEOUT_SECONDS = 60
STDOUT_EXCERPT_CHARS = 200


def read_archived_identity(root: Path) -> dict[str, Any]:
    """The archived checkout's identity, read from the file its own generator wrote."""
    identity = json.loads(
        contained_file(root, ARCHIVED_IDENTITY_RELATIVE).read_text(encoding="utf-8")
    )
    missing = [key for key in IDENTITY_KEYS if key not in identity]
    require(not missing, f"archived schema identity lacks {missing}")
    return {key: identity[key] for key in IDENTITY_KEYS}


def read_archived_gcode_version(root: Path) -> str:
    manifest = tomllib.loads(
        contained_file(root, ARCHIVED_GCODE_CRATE_RELATIVE).read_text(encoding="utf-8")
    )
    version = manifest.get("package", {}).get("version")
    require(isinstance(version, str), "archived gcode crate lacks package.version")
    return str(version)


def gcode_binary_relative() -> str:
    """The installed gcode in the gobby home bin set, the one binary step 3 installs."""
    # Deferred like _run_binary: importing this module in step 3 loads no gobby.
    from gobby.utils.native_bin import native_bin_name

    return f"{BINARY_DIR_RELATIVE}/{native_bin_name('gcode')}"


def check_schema_head(
    root: Path, *, gdaemon_identity: Mapping[str, Any], gcode_version: str
) -> dict[str, Any]:
    """Fail closed unless the installed set is the archived head; the leaf then waits for the Orchestrator."""
    archived = read_archived_identity(root)
    installed = {key: gdaemon_identity.get(key) for key in IDENTITY_KEYS}
    require(
        installed == archived,
        f"schema head mismatch: installed gdaemon {installed} differs from archived {archived}; "
        "stop the leaf and ask the Orchestrator for its next cutover",
    )
    crate_version = read_archived_gcode_version(root)
    require(
        gcode_version.strip() == f"gcode {crate_version}",
        f"installed gcode {gcode_version.strip()!r} is not the crates/gcode version {crate_version!r}",
    )
    return archived


def _run_binary(root: Path, name: str, arguments: list[str]) -> str:
    # Deferred: prepare_daemon imports validate_environment, which imports this module. The
    # gobby import is deferred too, so importing this module in step 3 loads no gobby.
    from prepare_daemon import isolated_environment

    from gobby.utils.native_bin import native_bin_name

    binary = contained_file(root, f"{BINARY_DIR_RELATIVE}/{native_bin_name(name)}")
    completed = subprocess.run(
        [str(binary), *arguments],
        cwd=root / "gobby-home",
        env=isolated_environment(root, dict(os.environ)),
        capture_output=True,
        text=True,
        check=False,
        timeout=VERSION_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise EvidenceError(
            f"{name} {' '.join(arguments)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _gdaemon_identity(root: Path) -> dict[str, Any]:
    output = _run_binary(root, "gdaemon", ["schema", "version", "--json"])
    excerpt = output[:STDOUT_EXCERPT_CHARS]
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise EvidenceError(
            f"gdaemon schema version --json printed malformed JSON: {excerpt!r}"
        ) from error
    require(
        isinstance(payload, dict),
        f"gdaemon schema version --json printed a non-object payload: {excerpt!r}",
    )
    return payload


def schema_head_proof(root: Path) -> dict[str, Any]:
    """Read the installed set's own identity and version, then check them against the archive."""
    gdaemon_identity = _gdaemon_identity(root)
    gcode_version = _run_binary(root, "gcode", ["--version"])
    archived = check_schema_head(
        root, gdaemon_identity=gdaemon_identity, gcode_version=gcode_version
    )
    return {"identity": archived, "gcode_version": gcode_version.strip()}
