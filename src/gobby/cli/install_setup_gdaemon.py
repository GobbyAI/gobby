"""Fatal installer and identity verifier for the gdaemon schema authority."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

from gobby.cli.install_setup_versions import managed_version_satisfies_pin
from gobby.install.bin_set_coherence import promote_workspace_binary_set
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.storage.schema_contract import (
    expected_schema_identity,
    expected_schema_identity_json,
)

_BINARY_NAME = "gdaemon.exe" if sys.platform == "win32" else "gdaemon"
_VERSION_STAMP = ".gdaemon-version"
_IDENTITY_STAMP = ".gdaemon-schema-identity.json"
_TARGETS = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("win32", "amd64"): "x86_64-pc-windows-msvc",
    ("win32", "arm64"): "aarch64-pc-windows-msvc",
}


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


def _install_from_workspace(binary: Path) -> bool:
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
    source = manifest.parent / "target" / "release" / _BINARY_NAME
    if result.returncode != 0 or not source.is_file():
        return False
    promote_workspace_binary_set({"gdaemon": source}, bin_dir=binary.parent)
    return True


def _install_from_release(binary: Path, version: str) -> bool:
    target = _TARGETS.get((sys.platform, platform.machine().lower()))
    if target is None:
        return False
    from gobby.cli.install_setup import _download_release_binary

    return bool(
        _download_release_binary(
            binary.parent,
            binary_name=_BINARY_NAME,
            artifact_name="gdaemon",
            target=target,
            version=version,
            tag_prefix="gdaemon-v",
            label="gdaemon",
        )
    )


def _codesign(binary: Path) -> None:
    if sys.platform != "darwin" or shutil.which("codesign") is None:
        return
    try:
        result = subprocess.run(
            ["codesign", "-f", "-s", "-", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise GdaemonInstallError(
            f"{binary.name} ad-hoc signing timed out after 30 seconds"
        ) from exc
    if result.returncode != 0:
        raise GdaemonInstallError(f"{binary.name} ad-hoc signing failed: {result.stderr.strip()}")


def _install_gdaemon(binary: Path, version: str) -> str:
    if _install_from_workspace(binary):
        method = "workspace"
    elif _install_from_release(binary, version):
        method = "github"
    else:
        raise GdaemonInstallError("all gdaemon installation methods failed")
    _codesign(binary)
    return method


def ensure_gdaemon(*, bin_dir: Path | None = None, force: bool = False) -> dict[str, object]:
    """Provision gdaemon and verify its same-binary identity before DB initialization."""
    directory = bin_dir or Path.home() / ".gobby" / "bin"
    binary = directory / _BINARY_NAME
    expected = expected_schema_identity()
    version = _probe_version(binary) if binary.is_file() else None
    identity = _probe_identity(binary) if binary.is_file() else None
    pin = MANAGED_BIN_VERSION_PINS["gdaemon"]

    if (
        not force
        and version is not None
        and managed_version_satisfies_pin("gdaemon", version)
        and identity == expected
    ):
        _write_sidecars(directory, version)
        return {"installed": False, "version": version, "method": "existing"}

    directory.mkdir(parents=True, exist_ok=True)
    method = _install_gdaemon(binary, pin)
    version = _probe_version(binary)
    identity = _probe_identity(binary)
    if version is None or not managed_version_satisfies_pin("gdaemon", version):
        raise GdaemonInstallError(f"installed gdaemon does not satisfy required version {pin}")
    if identity != expected:
        raise GdaemonInstallError(
            f"installed gdaemon schema identity mismatch: expected {expected}, observed {identity}"
        )
    _write_sidecars(directory, version)
    return {"installed": True, "version": version, "method": method}


def _write_sidecars(directory: Path, version: str) -> None:
    (directory / _VERSION_STAMP).write_text(f"{version}\n", encoding="utf-8")
    (directory / _IDENTITY_STAMP).write_text(
        f"{expected_schema_identity_json()}\n",
        encoding="utf-8",
    )
