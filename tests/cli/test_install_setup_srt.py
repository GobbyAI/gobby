"""Tests for installation of Gobby's pinned Sandbox Runtime."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from gobby.agents.srt_runtime import SrtInstallation, SrtRuntimeError
from gobby.cli import install_setup_srt
from gobby.utils.dependency_requirements import SRT_RELEASE


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
    class FakeResponse:
        def __init__(self) -> None:
            self._chunks = iter((b"not-the-pinned-tarball", b""))

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return next(self._chunks)

    monkeypatch.setattr(install_setup_srt, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(SrtRuntimeError, match="checksum mismatch"):
        install_setup_srt._download_verified_tarball(tmp_path / "runtime.tgz")


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
        if verify_calls == 1:
            raise SrtRuntimeError("not installed")
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
        install_setup_srt.shutil,
        "which",
        lambda command: str(npm) if command == "npm" else None,
    )
    monkeypatch.setattr(
        install_setup_srt,
        "_download_verified_tarball",
        lambda destination: destination.write_bytes(b"verified tarball"),
    )
    monkeypatch.setattr(install_setup_srt.subprocess, "run", fake_npm_run)

    result = install_setup_srt.install_srt_runtime()

    assert result.installed is True
    assert result.path == target.resolve()
    assert verify_calls == 2
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
    assert (target / "runner.mjs").stat().st_mode & 0o777 == 0o444
    assert (target / "content-manifest.json").is_file()
    assert target.stat().st_mode & 0o777 == 0o555
    receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["package"] == SRT_RELEASE.package
    assert receipt["version"] == SRT_RELEASE.version
    install_lock.__enter__.assert_called_once_with()
    install_lock.__exit__.assert_called_once()
