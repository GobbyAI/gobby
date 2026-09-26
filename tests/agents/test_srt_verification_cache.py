"""Cache boundaries for managed Sandbox Runtime installation verification."""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from gobby.agents import srt_runtime
from gobby.agents.srt_runtime import SrtInstallation, SrtRuntimeError
from gobby.utils.dependency_requirements import SRT_RELEASE

pytestmark = pytest.mark.unit


@pytest.fixture
def installed_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "srt"
    root.mkdir()
    (root / "runner.mjs").write_text("runtime", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("node", "gcode", "gdaemon", "ghook"):
        binary = bin_dir / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    (bin_dir / ".gdaemon-schema-identity.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(srt_runtime, "srt_install_lock", nullcontext)
    monkeypatch.setattr(srt_runtime, "_verified_srt_cache", None, raising=False)
    return root, bin_dir, bin_dir / "node"


def _counting_verifier(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    node: Path,
) -> list[int]:
    calls: list[int] = []

    def verify(**_context: str | None) -> SrtInstallation:
        calls.append(1)
        return SrtInstallation(
            root=root,
            node=node,
            runner=root / "runner.mjs",
            package_json=root / "package.json",
        )

    monkeypatch.setattr(srt_runtime, "verify_srt_installation_locked", verify)
    return calls


def test_repeat_verification_hits_cache_and_logs_key(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root, _, node = installed_runtime
    calls = _counting_verifier(monkeypatch, root, node)
    caplog.set_level(logging.INFO, logger=srt_runtime.__name__)

    first = srt_runtime.verify_srt_installation()
    second = srt_runtime.verify_srt_installation()

    assert first is second
    assert len(calls) == 1
    assert "cache hit key=" in caplog.text


@pytest.mark.parametrize("member", ["node", "gcode", "gdaemon", "ghook"])
def test_binary_replacement_invalidates_cache(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    member: str,
) -> None:
    root, bin_dir, node = installed_runtime
    calls = _counting_verifier(monkeypatch, root, node)
    srt_runtime.verify_srt_installation()
    replacement = bin_dir / f".{member}.new"
    replacement.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    replacement.chmod(0o755)
    replacement.replace(bin_dir / member)

    srt_runtime.verify_srt_installation()

    assert len(calls) == 2


def test_release_and_installed_tree_changes_invalidate_cache(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, node = installed_runtime
    calls = _counting_verifier(monkeypatch, root, node)
    srt_runtime.verify_srt_installation()
    monkeypatch.setattr(srt_runtime, "SRT_RELEASE", replace(SRT_RELEASE, version="next-version"))
    srt_runtime.verify_srt_installation()
    (root / "runner.mjs").write_text("changed runtime", encoding="utf-8")

    srt_runtime.verify_srt_installation()

    assert len(calls) == 3


def test_verification_failure_never_enters_cache(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, node = installed_runtime
    calls: list[int] = []

    def verify(**_context: str | None) -> SrtInstallation:
        calls.append(1)
        if len(calls) == 1:
            raise SrtRuntimeError("invalid sandbox runtime")
        return SrtInstallation(
            root=root,
            node=node,
            runner=root / "runner.mjs",
            package_json=root / "package.json",
        )

    monkeypatch.setattr(srt_runtime, "verify_srt_installation_locked", verify)
    with pytest.raises(SrtRuntimeError, match="invalid sandbox runtime"):
        srt_runtime.verify_srt_installation()
    srt_runtime.verify_srt_installation()
    srt_runtime.verify_srt_installation()

    assert len(calls) == 2


def test_missing_binary_never_uses_stale_cache(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bin_dir, node = installed_runtime
    calls = _counting_verifier(monkeypatch, root, node)
    srt_runtime.verify_srt_installation()
    (bin_dir / "gdaemon").unlink()

    def refuse(**_context: str | None) -> SrtInstallation:
        calls.append(1)
        raise SrtRuntimeError("full verification required")

    monkeypatch.setattr(srt_runtime, "verify_srt_installation_locked", refuse)
    with pytest.raises(SrtRuntimeError, match="full verification required"):
        srt_runtime.verify_srt_installation()

    assert len(calls) == 2
