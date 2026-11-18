"""Fatal installer and identity verifier for the gdaemon schema authority."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from gobby.agents.cargo_target import cargo_release_dir
from gobby.cli.install_setup_versions import managed_version_satisfies_pin
from gobby.install.bin_generations import promotion_guard
from gobby.install.bin_release_identity import BinaryReleaseVersionError
from gobby.install.bin_set_coherence import (
    BinarySetCoherenceError,
    promote_workspace_binary_set,
    workspace_build_only,
    write_atomic_bytes,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.paths import get_gobby_home
from gobby.storage.schema_contract import (
    expected_schema_identity,
    expected_schema_identity_json,
)

_BINARY_NAME = "gdaemon.exe" if sys.platform == "win32" else "gdaemon"
_VERSION_STAMP = ".gdaemon-version"
_IDENTITY_STAMP = ".gdaemon-schema-identity.json"


class GdaemonInstallError(RuntimeError):
    """Raised when install cannot produce the exact packaged schema authority."""


def _probe_version(binary: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    return parts[-1] if parts else None


def _probe_identity(binary: Path) -> dict[str, int | str] | None:
    try:
        result = subprocess.run(
            [str(binary), "schema", "version", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        parsed: object = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, int | str], parsed)


def _workspace_manifest() -> Path | None:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        manifest = parent / "Cargo.toml"
        if manifest.is_file() and (parent / "crates" / "gdaemon" / "Cargo.toml").is_file():
            return manifest
    return None


def _install_from_workspace(
    binary: Path, verify: Callable[[Path], None], settle: Callable[[], None]
) -> bool:
    manifest = _workspace_manifest()
    if manifest is None or shutil.which("cargo") is None:
        return False
    try:
        result = subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "gobby-daemon",
                "--manifest-path",
                str(manifest),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        raise GdaemonInstallError(
            f"gdaemon cargo build failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    source = cargo_release_dir(manifest.parent) / _BINARY_NAME
    if not source.is_file():
        return False
    try:
        promote_workspace_binary_set(
            {"gdaemon": source},
            bin_dir=binary.parent,
            prepare_staged={"gdaemon": verify},
            after_swap=settle,
        )
    except (BinarySetCoherenceError, BinaryReleaseVersionError) as exc:
        raise GdaemonInstallError(str(exc)) from exc
    return True


def _install_gdaemon(
    binary: Path, verify: Callable[[Path], None], settle: Callable[[], None]
) -> str:
    if not _install_from_workspace(binary, verify, settle):
        raise GdaemonInstallError(workspace_build_only("gdaemon"))
    return "workspace"


def ensure_gdaemon(*, bin_dir: Path | None = None, force: bool = False) -> dict[str, object]:
    """Provision gdaemon and verify its same-binary identity before DB initialization."""
    directory = bin_dir if bin_dir is not None else get_gobby_home() / "bin"
    binary = directory / _BINARY_NAME
    expected = expected_schema_identity()
    version = _probe_version(binary) if binary.is_file() else None
    identity = _probe_identity(binary) if binary.is_file() else None

    if (
        not force
        and version is not None
        and managed_version_satisfies_pin("gdaemon", version)
        and identity == expected
    ):
        # No swap happens on this path, so the stamp takes the store lock itself.
        with promotion_guard(directory, []):
            _write_sidecars(directory, version)
        return {"installed": False, "version": version, "method": "existing"}

    directory.mkdir(parents=True, exist_ok=True)
    settled: dict[str, str] = {}

    def verify_candidate(staged: Path) -> None:
        settled["version"] = _verify_candidate(staged, expected)

    def settle_installed() -> None:
        _write_sidecars(directory, settled["version"])

    method = _install_gdaemon(binary, verify_candidate, settle_installed)
    return {"installed": True, "version": settled["version"], "method": method}


def _verify_candidate(candidate: Path, expected: dict[str, int | str]) -> str:
    """Probe the staged gdaemon before the swap; returns the version its sidecar will record.

    The promotion runs this on the staged copy with no store lock held, so a candidate that
    fails the pin or the schema identity is refused before any installed binary is replaced.
    """
    version = _probe_version(candidate)
    identity = _probe_identity(candidate)
    pin = MANAGED_BIN_VERSION_PINS["gdaemon"]
    if version is None or not managed_version_satisfies_pin("gdaemon", version):
        raise GdaemonInstallError(f"gdaemon does not satisfy required version {pin}")
    if identity != expected:
        raise GdaemonInstallError(
            f"gdaemon schema identity mismatch: expected {expected}, observed {identity}"
        )
    return version


def _write_sidecars(directory: Path, version: str) -> None:
    """Replace both stamps in one rename each, so a concurrent capture never reads a torn file."""
    write_atomic_bytes(directory / _VERSION_STAMP, f"{version}\n".encode(), mode=0o644)
    write_atomic_bytes(
        directory / _IDENTITY_STAMP,
        f"{expected_schema_identity_json()}\n".encode(),
        mode=0o644,
    )
