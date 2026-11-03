"""Tests for the sandboxed-SRT ``ps`` shim.

TODO(#22407, #25069): delete this module together with ``gobby.agents.droid_ps_shim``
once no sandboxed seat needs a ``ps`` binary.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 # asserts on a locally re-signed system binary.
import sys
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import droid_ps_shim
from gobby.agents.droid_ps_shim import PS_WRAPPER, droid_ps_shim_dir, ps_shim_path_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUST_WRAPPER = _REPO_ROOT / "crates/gdaemon/src/nodes/agents/sandbox/ps_shim_wrapper.sh"

pytestmark = pytest.mark.unit


def _stub_darwin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, list[list[str]]]:
    """Point the shim at a fake system binary and a recording codesign."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(droid_ps_shim, "get_gobby_home", lambda: tmp_path)
    source = tmp_path / "system-ps"
    source.write_bytes(b"system ps bytes")
    monkeypatch.setattr(droid_ps_shim, "SYSTEM_PS", source)
    codesign = tmp_path / "codesign"
    codesign.write_text("", encoding="utf-8")
    monkeypatch.setattr(droid_ps_shim, "CODESIGN", codesign)

    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # The patch is process-wide: pass other callers through, or their argv[-1]
        # lands as a file in the working directory.
        if argv[:1] != [str(codesign)]:
            return real_run(argv, **kwargs)
        calls.append(argv)
        Path(argv[-1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("gobby.utils.spawn.run", fake_run)
    return source, calls


class TestPsShimPathEnv:
    def test_contributes_nothing_without_a_shim_directory(self) -> None:
        assert ps_shim_path_env(None, {"PATH": "/usr/bin"}) == {}

    def test_prepends_the_shim_directory_so_the_supervisor_resolves_it_first(self) -> None:
        assert ps_shim_path_env(Path("/shim"), {"PATH": "/usr/bin:/bin"}) == {
            "PATH": f"/shim{os.pathsep}/usr/bin:/bin"
        }

    def test_uses_the_shim_directory_alone_when_path_is_unset(self) -> None:
        assert ps_shim_path_env(Path("/shim"), {}) == {"PATH": "/shim"}


class TestDroidPsShimDir:
    def test_is_unavailable_off_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert droid_ps_shim_dir() is None

    def test_is_unavailable_without_codesign(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_darwin(monkeypatch, tmp_path)
        monkeypatch.setattr(droid_ps_shim, "CODESIGN", tmp_path / "absent")
        assert droid_ps_shim_dir() is None

    def test_builds_an_executable_copy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, calls = _stub_darwin(monkeypatch, tmp_path)
        directory = droid_ps_shim_dir()
        assert directory is not None
        shim = directory / "ps"
        assert shim.is_file()
        assert shim.stat().st_mode & 0o111
        assert shim.read_text(encoding="utf-8") == PS_WRAPPER
        assert (directory / "bin" / "ps").read_bytes() == b"signed"
        assert len(calls) == 1
        assert calls[0][0] == str(droid_ps_shim.CODESIGN)
        assert calls[0][1:4] == ["-f", "-s", "-"]
        assert calls[0][4].startswith(str(directory / "bin" / ".ps."))

    def test_only_the_wrapper_is_executable_at_the_top_of_the_shim_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The unrestricted copy stays off PATH, so the wrapper is the one ps a seat reaches."""
        _stub_darwin(monkeypatch, tmp_path)
        directory = droid_ps_shim_dir()
        assert directory is not None
        executables = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_file() and os.access(entry, os.X_OK)
        )
        assert executables == ["ps"]

    def test_reuses_the_cached_copy_instead_of_re_signing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, calls = _stub_darwin(monkeypatch, tmp_path)
        first = droid_ps_shim_dir()
        second = droid_ps_shim_dir()
        assert first == second
        assert len(calls) == 1, "a cached shim must not re-run codesign on every spawn"

    def test_a_replaced_system_binary_lands_in_a_new_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        source, calls = _stub_darwin(monkeypatch, tmp_path)
        before = droid_ps_shim_dir()
        source.write_bytes(b"system ps bytes after an OS update")
        after = droid_ps_shim_dir()
        assert before is not None
        assert after is not None
        assert before != after, "a new /bin/ps digest must not serve the stale signed copy"
        assert len(calls) == 2

    def test_a_codesign_failure_fails_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_darwin(monkeypatch, tmp_path)

        def failing_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr("gobby.utils.spawn.run", failing_run)
        # A broken shim must never break the spawn; Droid degrades to today's behavior.
        assert droid_ps_shim_dir() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the shim re-signs a macOS system binary")
def test_the_real_shim_answers_the_supervisor_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plain copy of /bin/ps is killed by AMFI; the re-signed copy must run."""
    monkeypatch.setattr(droid_ps_shim, "get_gobby_home", lambda: tmp_path)
    directory = droid_ps_shim_dir()
    assert directory is not None

    probe = subprocess.run(  # nosec B603 # fixed argv, locally built shim.
        [str(directory / "ps"), "-o", "ppid=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == str(os.getppid())


def test_wrapper_mirrors_the_rust_embedded_source() -> None:
    """The Rust daemon embeds ps_shim_wrapper.sh; the Python copy must stay byte-identical."""
    assert PS_WRAPPER == _RUST_WRAPPER.read_text(encoding="utf-8")


@pytest.fixture
def shim_dir(tmp_path: Path) -> Path:
    """A shim directory with the wrapper and a stub binary that echoes its argv."""
    directory = tmp_path / "shim"
    directory.mkdir()
    wrapper = directory / "ps"
    wrapper.write_text(PS_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    real_dir = directory / "bin"
    real_dir.mkdir()
    real = real_dir / "ps"
    real.write_text('#!/bin/sh\necho "ran $*"\n', encoding="utf-8")
    real.chmod(0o755)
    return directory


def _run_wrapper(directory: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 # fixed argv, a stub shim in the test's tmp dir.
        [str(directory / "ps"), *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["-axo", "pid,ppid,etime,comm"],
        ["-o", "ppid=", "-p", "4242"],  # Droid's supervisor probe.
        ["-axo", "pid="],
        ["-p", "123", "-o", "pid,etime"],
        ["-ww", "-o", "pid,etime,user"],
        ["-o", "lstart,pcpu,rss"],
    ],
)
def test_wrapper_runs_the_allowed_listings(shim_dir: Path, argv: list[str]) -> None:
    result = _run_wrapper(shim_dir, argv)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"ran {' '.join(argv)}\n"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["aux"],
        ["-e"],
        ["-o", "args"],
        ["-axo", "command"],
        ["-o", "comm,args"],
        ["-E", "-o", "pid"],
        ["-o"],
        ["-o", "pid,"],
        ["-p", "-o", "pid"],
        ["-o", "pid", "-e"],
        ["-o", "pid", "--", "x"],
    ],
)
def test_wrapper_refuses_anything_outside_the_allow_list(shim_dir: Path, argv: list[str]) -> None:
    result = _run_wrapper(shim_dir, argv)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "refused" in result.stderr
