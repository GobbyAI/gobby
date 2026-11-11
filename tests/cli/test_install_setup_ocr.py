"""The managed OCR CLI installs the pinned release and removes any install it cannot verify."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gobby.cli.install_setup_ocr import OcrInstallError, install_ocr_cli
from gobby.utils import dependency_requirements as requirements
from gobby.utils import spawn
from gobby.utils.dependency_requirements import OCR_PACKAGE, OCR_VERSION

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


@dataclass
class FakeNpm:
    """Stands in for npm: `install -g` lays out `tree` under `prefix`, `uninstall -g` removes it."""

    prefix: Path
    npm_dir: Path
    tree: dict[str, bytes]
    native: bytes | None = NATIVE_BYTES
    returncode: int = 0
    stderr: str = ""
    calls: list[list[str]] = field(default_factory=list)

    @property
    def npm(self) -> str:
        return str(self.npm_dir / "npm")

    def installs(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:2] == ["install"]]

    def uninstalls(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:2] == ["uninstall"]]


@pytest.fixture
def npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNpm:
    """Pinned release pins from the golden bytes; PATH holds npm only until an install lands."""
    npm_dir = tmp_path / "npm-bin"
    npm_dir.mkdir()
    (npm_dir / "npm").write_text("#!/bin/sh\n")
    (npm_dir / "npm").chmod(0o755)
    fake = FakeNpm(prefix=tmp_path / "prefix", npm_dir=npm_dir, tree=_launcher_tree())

    monkeypatch.setattr(requirements, "_ocr_platform_key", lambda: PLATFORM)
    monkeypatch.setattr(
        requirements,
        "OCR_LAUNCHER_SHA256",
        {relative: _sha256(data) for relative, data in _launcher_tree().items()},
    )
    monkeypatch.setattr(requirements, "OCR_NATIVE_SHA256", {PLATFORM: _sha256(NATIVE_BYTES)})
    monkeypatch.setenv("PATH", str(npm_dir))

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        fake.calls.append(list(argv))
        if argv[1:2] == ["install"]:
            if fake.returncode == 0:
                _write_npm_install(fake.prefix, fake.tree, fake.native)
                monkeypatch.setenv("PATH", f"{fake.prefix / 'bin'}{os.pathsep}{npm_dir}")
            return subprocess.CompletedProcess(argv, fake.returncode, stdout="", stderr=fake.stderr)
        if argv[1:2] == ["uninstall"]:
            shutil.rmtree(_package_root(fake.prefix), ignore_errors=True)
            (fake.prefix / "bin" / "ocr").unlink(missing_ok=True)
            monkeypatch.setenv("PATH", str(npm_dir))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(spawn, "run", run)
    return fake


def test_pinned_install_is_left_in_place_on_rerun(
    npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_npm_install(npm.prefix, npm.tree, npm.native)
    monkeypatch.setenv("PATH", str(npm.prefix / "bin"))

    result = install_ocr_cli()

    assert result.installed is False
    assert result.version == OCR_VERSION
    assert result.path == (npm.prefix / "bin" / "ocr").resolve()
    assert npm.installs() == []
    assert npm.uninstalls() == []


def test_missing_ocr_installs_the_pinned_release(npm: FakeNpm) -> None:
    result = install_ocr_cli()

    assert result.installed is True
    assert result.version == OCR_VERSION
    assert result.path == (npm.prefix / "bin" / "ocr").resolve()
    assert npm.installs() == [
        [npm.npm, "install", "-g", "--ignore-scripts", f"{OCR_PACKAGE}@{OCR_VERSION}"]
    ]
    assert npm.uninstalls() == []


def test_outdated_install_is_replaced_with_the_pin(
    npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_npm_install(npm.prefix, _launcher_tree("1.12.0"), NATIVE_BYTES)
    monkeypatch.setenv("PATH", f"{npm.prefix / 'bin'}{os.pathsep}{npm.npm_dir}")

    result = install_ocr_cli()

    assert result.installed is True
    assert result.version == OCR_VERSION
    assert len(npm.installs()) == 1
    assert npm.uninstalls() == []


def test_npm_failure_is_an_install_error(npm: FakeNpm) -> None:
    npm.returncode = 1
    npm.stderr = "npm ERR! ENOTFOUND registry.npmjs.org"

    with pytest.raises(OcrInstallError, match="ENOTFOUND"):
        install_ocr_cli()


def test_install_yielding_another_version_is_removed(npm: FakeNpm) -> None:
    npm.tree = _launcher_tree("9.9.9")

    with pytest.raises(OcrInstallError, match="9.9.9"):
        install_ocr_cli()

    assert npm.uninstalls() == [[npm.npm, "uninstall", "-g", OCR_PACKAGE]]
    assert not (npm.prefix / "bin" / "ocr").exists()


def test_install_with_a_tampered_launcher_is_removed(npm: FakeNpm) -> None:
    npm.tree = {**_launcher_tree(), "scripts/update.js": b"// unpinned\n"}

    with pytest.raises(OcrInstallError, match="scripts/update.js"):
        install_ocr_cli()

    assert npm.uninstalls() == [[npm.npm, "uninstall", "-g", OCR_PACKAGE]]
    assert not (npm.prefix / "bin" / "ocr").exists()


def test_install_with_a_substituted_native_binary_is_removed(npm: FakeNpm) -> None:
    npm.native = b"substituted\n"

    with pytest.raises(OcrInstallError, match="opencodereview"):
        install_ocr_cli()

    assert npm.uninstalls() == [[npm.npm, "uninstall", "-g", OCR_PACKAGE]]


def test_missing_npm_is_an_install_error(npm: FakeNpm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(npm.prefix))

    with pytest.raises(OcrInstallError, match="npm is required"):
        install_ocr_cli()

    assert npm.calls == []
