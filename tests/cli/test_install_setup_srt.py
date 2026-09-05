"""Tests for installation of Gobby's pinned Sandbox Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.error import URLError
from urllib.request import Request

import pytest

from gobby.agents import srt_runtime
from gobby.agents.srt_runtime import SrtInstallation, SrtRuntimeError
from gobby.cli import install_setup_srt
from gobby.utils.dependency_requirements import SRT_RELEASE, DependencyStatus


class FakeDownloadResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = iter((content, b""))
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> FakeDownloadResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return next(self._chunks)


def test_install_srt_runtime_wraps_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_install() -> None:
        raise URLError("offline")

    monkeypatch.setattr(install_setup_srt, "_install_srt_runtime", fail_install)

    with pytest.raises(SrtRuntimeError, match="failed to install managed SRT"):
        install_setup_srt.install_srt_runtime()


def test_download_verified_tarball_rejects_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        install_setup_srt,
        "urlopen",
        lambda *_args, **_kwargs: FakeDownloadResponse(b"not-the-pinned-tarball"),
    )

    with pytest.raises(SrtRuntimeError, match="checksum mismatch"):
        install_setup_srt._download_verified_tarball(tmp_path / "runtime.tgz")


def test_download_verified_tarball_retries_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"verified tarball"
    responses = iter(
        (
            FakeDownloadResponse(b"wrong tarball"),
            FakeDownloadResponse(expected),
        )
    )
    monkeypatch.setattr(
        install_setup_srt,
        "SRT_RELEASE",
        replace(SRT_RELEASE, tarball_sha256=hashlib.sha256(expected).hexdigest()),
    )
    monkeypatch.setattr(install_setup_srt, "urlopen", lambda *_args, **_kwargs: next(responses))
    destination = tmp_path / "runtime.tgz"

    install_setup_srt._download_verified_tarball(destination)

    assert destination.read_bytes() == expected


def test_download_verified_tarball_retries_incomplete_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"verified tarball"
    split = 7
    responses = iter(
        (
            FakeDownloadResponse(
                expected[:split],
                headers={"Content-Length": str(len(expected))},
            ),
            FakeDownloadResponse(
                expected,
                headers={"Content-Length": str(len(expected))},
            ),
        )
    )
    requests: list[Request] = []

    def respond(request: Request, **_kwargs: object) -> FakeDownloadResponse:
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(
        install_setup_srt,
        "SRT_RELEASE",
        replace(SRT_RELEASE, tarball_sha256=hashlib.sha256(expected).hexdigest()),
    )
    monkeypatch.setattr(install_setup_srt, "urlopen", respond)
    destination = tmp_path / "runtime.tgz"

    install_setup_srt._download_verified_tarball(destination)

    assert destination.read_bytes() == expected
    assert len(requests) == 2
    assert requests[0].get_header("Range") is None
    assert requests[1].get_header("Range") is None


def test_download_verified_tarball_rejects_non_https_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        install_setup_srt,
        "SRT_RELEASE",
        replace(SRT_RELEASE, tarball_url="file:///tmp/srt.tgz"),
    )

    with pytest.raises(SrtRuntimeError, match="must use HTTPS"):
        install_setup_srt._download_verified_tarball(tmp_path / "sandbox-runtime.tgz")


def test_install_srt_runtime_uses_locked_npm_ci_and_promotes_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "tools" / "srt" / SRT_RELEASE.version
    node = tmp_path / "bin" / "node"
    npm = tmp_path / "bin" / "npm"
    node.parent.mkdir()
    node.write_text("node", encoding="utf-8")
    npm.write_text("npm", encoding="utf-8")
    verify_calls = 0

    def fake_verify() -> SrtInstallation:
        nonlocal verify_calls
        verify_calls += 1
        assert target.exists()
        return SrtInstallation(
            root=target,
            node=node,
            runner=target / "runner.mjs",
            package_json=(
                target / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
            ),
        )

    npm_commands: list[tuple[list[str], Path]] = []
    install_lock = MagicMock()

    def fake_npm_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        cwd = Path(kwargs["cwd"])
        npm_commands.append((command, cwd))
        package_dir = cwd / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(install_setup_srt, "verify_srt_installation_locked", fake_verify)
    monkeypatch.setattr(install_setup_srt, "srt_install_lock", lambda: install_lock)
    monkeypatch.setattr(install_setup_srt, "srt_install_root", lambda: target)
    monkeypatch.setattr(install_setup_srt, "_require_node", lambda: node)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: str(npm) if command == "npm" else None,
    )
    monkeypatch.setattr(
        install_setup_srt,
        "_download_verified_tarball",
        lambda destination: destination.write_bytes(b"verified tarball"),
    )
    monkeypatch.setattr(subprocess, "run", fake_npm_run)

    result = install_setup_srt.install_srt_runtime()

    assert result.installed is True
    assert result.path == target.resolve()
    assert verify_calls == 1
    assert len(npm_commands) == 1
    command, staging = npm_commands[0]
    assert command == [
        str(npm.resolve()),
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--omit=dev",
    ]
    assert not staging.exists()
    assert (target / "runner.mjs").is_file()
    assert (target / "runner.mjs").stat().st_mode & 0o777 == 0o555
    assert (target / "content-manifest.json").is_file()
    assert target.stat().st_mode & 0o777 == 0o555
    receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["package"] == SRT_RELEASE.package
    assert receipt["version"] == SRT_RELEASE.version
    install_lock.__enter__.assert_called_once_with()
    install_lock.__exit__.assert_called_once()


def _write_runtime_tree(root: Path, marker: str) -> None:
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
        encoding="utf-8",
    )
    for architecture in ("arm64", "x64"):
        helper = package_dir / "vendor" / "seccomp" / architecture / "apply-seccomp"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"executable helper")
    shutil.copyfile(Path(srt_runtime.__file__).with_name("srt_runner.mjs"), root / "runner.mjs")
    shutil.copyfile(
        Path(srt_runtime.__file__).parents[1] / "install" / "srt-package-lock.json",
        root / "package-lock.json",
    )
    (root / "receipt.json").write_text(
        json.dumps(SRT_RELEASE.receipt_fields() | {"node": "/usr/bin/node"}), encoding="utf-8"
    )
    (root / "marker").write_text(marker, encoding="utf-8")
    srt_runtime.write_srt_content_manifest(root)
    srt_runtime.make_srt_installation_immutable(root)


@pytest.fixture
def installed_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "tools" / "srt" / SRT_RELEASE.version
    _write_runtime_tree(target, "old")
    monkeypatch.setattr(install_setup_srt, "srt_install_root", lambda: target)
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: target)
    monkeypatch.setattr(
        srt_runtime,
        "node_dependency_status",
        lambda: DependencyStatus(
            state="healthy",
            installed_version="20.11.0",
            minimum_version="20.11.0",
            expected_version=None,
            path="/usr/bin/node",
            error=None,
        ),
    )
    return target


def test_promote_replaces_immutable_runtime(installed_runtime: Path) -> None:
    target = installed_runtime
    backup = target.with_name(f".{target.name}.previous")
    staging = target.with_name(".staging")
    _write_runtime_tree(backup, "stale")
    _write_runtime_tree(staging, "new")

    install_setup_srt._promote_install(staging, target)

    assert not backup.exists()
    assert not staging.exists()
    assert (target / "marker").read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o555
    assert (target / "marker").stat().st_mode & 0o777 == 0o444
    assert srt_runtime.verify_srt_installation().root == target.resolve()


@pytest.mark.parametrize("backup_kind", ["immutable", "symlink", "dangling_symlink"])
def test_verified_install_retries_backup_cleanup(
    installed_runtime: Path, tmp_path: Path, backup_kind: str
) -> None:
    target = installed_runtime
    backup = target.with_name(f".{target.name}.previous")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    sentinel = outside / "sentinel"
    outside.chmod(0o700)
    sentinel.write_bytes(b"keep outside content")
    sentinel.chmod(0o400)
    outside.chmod(0o500)
    if backup_kind == "immutable":
        backup.mkdir()
        (backup / "outside").symlink_to(outside, target_is_directory=True)
        (backup / "payload").write_bytes(b"old installation")
        srt_runtime.make_srt_installation_immutable(backup)
    else:
        backup.symlink_to(
            outside if backup_kind == "symlink" else tmp_path / "absent",
            target_is_directory=True,
        )

    result = install_setup_srt.install_srt_runtime()

    assert result.installed is False
    assert not os.path.lexists(backup)
    assert sentinel.read_bytes() == b"keep outside content"
    assert sentinel.stat().st_mode & 0o777 == 0o400
    assert outside.stat().st_mode & 0o777 == 0o500
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert target.stat().st_mode & 0o777 == 0o555
    assert srt_runtime.verify_srt_installation().root == target.resolve()


def test_promote_failure_restores_immutable_install(
    installed_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = installed_runtime
    staging = target.with_name(".staging")
    backup = target.with_name(f".{target.name}.previous")
    _write_runtime_tree(staging, "new")
    rename = Path.rename

    def fail_staging_rename(path: Path, destination: Path) -> Path:
        if path == staging:
            raise OSError("injected promotion failure")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_staging_rename)

    with pytest.raises(OSError, match="injected promotion failure"):
        install_setup_srt._promote_install(staging, target)

    assert not backup.exists()
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert staging.stat().st_mode & 0o777 == 0o555
    assert srt_runtime.verify_srt_installation().root == target.resolve()


def test_install_retries_interrupted_cleanup_after_promotion(
    installed_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = installed_runtime
    staging = target.with_name(".staging")
    backup = target.with_name(f".{target.name}.previous")
    _write_runtime_tree(staging, "new")
    rmtree = shutil.rmtree
    interrupted = False

    def interrupt_cleanup(path: Path) -> None:
        nonlocal interrupted
        if path == backup and not interrupted:
            interrupted = True
            raise PermissionError("injected cleanup interruption")
        rmtree(path)

    with monkeypatch.context() as cleanup_patch:
        cleanup_patch.setattr(shutil, "rmtree", interrupt_cleanup)
        with pytest.raises(PermissionError, match="injected cleanup interruption"):
            install_setup_srt._promote_install(staging, target)
        assert (backup / "marker").read_text(encoding="utf-8") == "old"
        assert (backup / "marker").stat().st_mode & 0o777 == 0o444
        assert srt_runtime.verify_srt_installation().root == target.resolve()

        result = install_setup_srt.install_srt_runtime()

    assert result.installed is False
    assert not backup.exists()
    assert not staging.exists()
    assert (target / "marker").read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o555
    assert srt_runtime.verify_srt_installation().root == target.resolve()


def test_install_cleans_immutable_staging_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tools" / "srt" / SRT_RELEASE.version
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep outside content")
    sentinel.chmod(0o400)
    outside.chmod(0o500)
    stages: list[Path] = []

    def fake_npm_run(
        command: list[str], *, cwd: Path, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        package_dir = cwd / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def fail_promotion(staging: Path, _target: Path) -> None:
        stages.append(staging)
        assert staging.stat().st_mode & 0o777 == 0o555
        staging.chmod(0o755)
        (staging / "outside").symlink_to(outside, target_is_directory=True)
        staging.chmod(0o555)
        raise SrtRuntimeError("injected install failure after hardening")

    monkeypatch.setattr(install_setup_srt, "srt_install_root", lambda: target)
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: target)
    monkeypatch.setattr(install_setup_srt, "_require_node", lambda: Path("/usr/bin/true"))
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr(
        install_setup_srt,
        "_download_verified_tarball",
        lambda destination: destination.write_bytes(b"verified tarball"),
    )
    monkeypatch.setattr(subprocess, "run", fake_npm_run)
    monkeypatch.setattr(install_setup_srt, "_promote_install", fail_promotion)

    with pytest.raises(SrtRuntimeError, match="injected install failure after hardening"):
        install_setup_srt.install_srt_runtime()

    assert len(stages) == 1
    assert not stages[0].exists()
    assert not target.exists()
    assert sentinel.read_bytes() == b"keep outside content"
    assert sentinel.stat().st_mode & 0o777 == 0o400
    assert outside.stat().st_mode & 0o777 == 0o500
