"""OCR status verifies the pinned npm files on PATH and never executes the CLI."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.utils import dependency_requirements as requirements
from gobby.utils import spawn
from gobby.utils.dependency_requirements import OCR_PACKAGE, OCR_VERSION, ocr_dependency_status

pytestmark = pytest.mark.unit

PLATFORM = "linux-x64"
NATIVE_BYTES = b"synthetic native binary\n"
LAUNCHER_BYTES: dict[str, bytes] = {
    "bin/ocr.js": b"// synthetic launcher\n",
    "scripts/install.js": b"// synthetic install\n",
    "scripts/platform.js": b"// synthetic platform\n",
    "scripts/update.js": b"// synthetic update\n",
    "scripts/version.js": b"// synthetic version\n",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package_json(version: str = OCR_VERSION, name: str = OCR_PACKAGE) -> bytes:
    return json.dumps({"name": name, "version": version}).encode()


def _launcher_tree(version: str = OCR_VERSION) -> dict[str, bytes]:
    return {"package.json": _package_json(version), **LAUNCHER_BYTES}


def _package_root(prefix: Path) -> Path:
    return prefix / "lib" / "node_modules" / "@alibaba-group" / "open-code-review"


def _native_binary(prefix: Path) -> Path:
    platform_dir = prefix / "lib" / "node_modules" / "@alibaba-group" / f"ocr-{PLATFORM}"
    return platform_dir / "bin" / "opencodereview"


def _write_npm_install(prefix: Path, tree: dict[str, bytes], native: bytes | None) -> None:
    """Lay out an npm global install: the package, its platform binary, and `prefix/bin/ocr`."""
    package_root = _package_root(prefix)
    for relative, data in tree.items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    launcher = package_root / "bin" / "ocr.js"
    launcher.chmod(0o755)
    if native is not None:
        binary = _native_binary(prefix)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(native)
        binary.chmod(0o755)
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "ocr"
    link.unlink(missing_ok=True)
    link.symlink_to(launcher)


@pytest.fixture
def prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pinned npm global install first on PATH. Any command execution fails the test."""
    monkeypatch.setattr(requirements, "_ocr_platform_key", lambda: PLATFORM)
    monkeypatch.setattr(
        requirements,
        "OCR_LAUNCHER_SHA256",
        {relative: _sha256(data) for relative, data in _launcher_tree().items()},
    )
    monkeypatch.setattr(requirements, "OCR_NATIVE_SHA256", {PLATFORM: _sha256(NATIVE_BYTES)})
    install_prefix = tmp_path / "prefix"
    _write_npm_install(install_prefix, _launcher_tree(), NATIVE_BYTES)
    monkeypatch.setenv("PATH", str(install_prefix / "bin"))

    def refuse(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"OCR status must not execute commands: {argv}")

    monkeypatch.setattr(spawn, "run", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    return install_prefix


def test_pinned_npm_install_is_healthy(prefix: Path) -> None:
    status = ocr_dependency_status()

    assert status.state == "healthy"
    assert status.installed_version == OCR_VERSION
    assert status.expected_version == OCR_VERSION
    assert status.path == str((prefix / "bin" / "ocr").resolve())
    assert status.error is None


def test_missing_ocr_names_the_install_action(
    prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    status = ocr_dependency_status()

    assert status.state == "missing"
    assert status.error is not None
    assert f"requires =={OCR_VERSION}" in status.error
    assert "gobby install" in status.error


def test_other_package_version_is_outdated(prefix: Path) -> None:
    _package_root(prefix).joinpath("package.json").write_bytes(_package_json("1.12.0"))

    status = ocr_dependency_status()

    assert status.state == "outdated"
    assert status.installed_version == "1.12.0"
    assert status.error is not None
    assert "1.12.0" in status.error


def test_tampered_launcher_file_is_invalid(prefix: Path) -> None:
    _package_root(prefix).joinpath("scripts", "update.js").write_bytes(b"// unpinned\n")

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None
    assert "scripts/update.js" in status.error


def test_tampered_native_binary_is_invalid(prefix: Path) -> None:
    _native_binary(prefix).write_bytes(b"substituted\n")

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None
    assert "opencodereview" in status.error


def test_missing_native_binary_is_invalid(prefix: Path) -> None:
    _native_binary(prefix).unlink()

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None
    assert "opencodereview" in status.error


def test_launcher_outside_the_npm_package_is_invalid(prefix: Path) -> None:
    link = prefix / "bin" / "ocr"
    link.unlink()
    link.write_bytes(b"#!/bin/sh\necho 'open-code-review v1.13.5'\n")
    link.chmod(0o755)

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None
    assert OCR_PACKAGE in status.error


def test_package_metadata_naming_another_package_is_invalid(prefix: Path) -> None:
    _package_root(prefix).joinpath("package.json").write_bytes(_package_json(name="other-ocr"))

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None
    assert OCR_PACKAGE in status.error


def test_unreadable_package_metadata_is_invalid(prefix: Path) -> None:
    _package_root(prefix).joinpath("package.json").write_bytes(b"{not json")

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None


def test_platform_without_a_pinned_build_is_invalid(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(requirements, "_ocr_platform_key", lambda: None)

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None
    assert "no pinned OCR build" in status.error


def test_report_includes_ocr_only_when_requested(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed: list[list[str]] = []

    def record(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        executed.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(spawn, "run", record)

    included = requirements.collect_dependency_report(
        managed_services=False, include_srt=False, include_ocr=True
    )
    excluded = requirements.collect_dependency_report(managed_services=False, include_srt=False)

    assert included.required["ocr"].state == "healthy"
    assert "ocr" not in excluded.required
    assert not any(Path(argv[0]).name == "ocr" for argv in executed)
