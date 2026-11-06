"""Cache boundaries for managed Sandbox Runtime installation verification."""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import srt_runtime
from gobby.agents.srt_runtime import SrtInstallation, SrtRuntimeError
from gobby.utils import dependency_requirements, spawn
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
        script = "#!/bin/sh\necho v22.12.0\n" if name == "node" else "#!/bin/sh\nexit 0\n"
        binary.write_text(script, encoding="utf-8")
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


@pytest.mark.parametrize(
    "member", ["node", "gcode", "gdaemon", "ghook", ".gdaemon-schema-identity.json"]
)
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


def test_binary_set_change_during_verification_locks_out_and_caches_nothing(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bin_dir, node = installed_runtime
    calls: list[int] = []

    def verify(**_context: str | None) -> SrtInstallation:
        calls.append(1)
        if len(calls) == 1:
            replacement = bin_dir / ".gdaemon.new"
            replacement.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            replacement.chmod(0o755)
            replacement.replace(bin_dir / "gdaemon")
        return SrtInstallation(
            root=root,
            node=node,
            runner=root / "runner.mjs",
            package_json=root / "package.json",
        )

    monkeypatch.setattr(srt_runtime, "verify_srt_installation_locked", verify)
    with pytest.raises(SrtRuntimeError, match="changed during verification"):
        srt_runtime.verify_srt_installation()
    srt_runtime.verify_srt_installation()

    assert len(calls) == 2


def test_bundled_runner_change_invalidates_cache(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, _, node = installed_runtime
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    bundled = package_dir / "srt_runner.mjs"
    bundled.write_text("runner v1", encoding="utf-8")
    monkeypatch.setattr(srt_runtime, "__file__", str(package_dir / "srt_runtime.py"))
    calls = _counting_verifier(monkeypatch, root, node)
    srt_runtime.verify_srt_installation()
    replacement = package_dir / ".srt_runner.mjs.new"
    replacement.write_text("runner v2", encoding="utf-8")
    replacement.replace(bundled)

    srt_runtime.verify_srt_installation()

    assert len(calls) == 2


STALL_SECONDS = 30.0


def _stall_first_exec_of(monkeypatch: pytest.MonkeyPatch, node: Path) -> set[str]:
    """Make the first exec of ``node`` stall for STALL_SECONDS, as a fresh binary can here.

    A call bounded below the stall times out. A call bounded above it completes, so the
    stall is paid there. Returns the set of node paths whose first exec completed.
    """
    node_path = str(node.resolve())
    completed_first_execs: set[str] = set()
    real_run = spawn.run

    def stalling_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if str(command[0]) == node_path and node_path not in completed_first_execs:
            if kwargs["timeout"] < STALL_SECONDS:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            completed_first_execs.add(node_path)
        return real_run(command, **kwargs)

    monkeypatch.setattr(spawn, "run", stalling_run)
    return completed_first_execs


def test_first_exec_stall_on_a_fresh_node_does_not_fail_cached_verification(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cache-hit path is the first node exec of this identity. Warm-up pays the stall
    # under its own bound, so the 10s probe measures only the probe.
    root, _, node = installed_runtime
    calls = _counting_verifier(monkeypatch, root, node)
    srt_runtime.verify_srt_installation()
    monkeypatch.setattr(dependency_requirements, "_healthy_node", None)
    monkeypatch.setattr(dependency_requirements, "_warmed_executables", set())
    completed = _stall_first_exec_of(monkeypatch, node)

    installation = srt_runtime.verify_srt_installation()

    assert installation.root == root
    assert str(node.resolve()) in completed
    assert len(calls) == 1


def test_first_exec_stall_fails_cached_verification_when_warm_up_is_bounded_below_it(
    installed_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: with warm-up bounded below the stall, the same stall reaches the timed probe.
    root, _, node = installed_runtime
    _counting_verifier(monkeypatch, root, node)
    srt_runtime.verify_srt_installation()
    monkeypatch.setattr(dependency_requirements, "_healthy_node", None)
    monkeypatch.setattr(dependency_requirements, "_warmed_executables", set())
    monkeypatch.setattr(dependency_requirements, "FIRST_EXEC_WARMUP_SECONDS", 10.0)
    _stall_first_exec_of(monkeypatch, node)

    with pytest.raises(SrtRuntimeError, match="timed out"):
        srt_runtime.verify_srt_installation()
