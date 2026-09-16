"""Tests for the sandboxed-Droid ``ps`` shim.

TODO(#22407): delete this module together with ``gobby.agents.droid_ps_shim``
once Factory fixes ``factory-execute-supervisor``.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 # asserts on a locally re-signed system binary.
import sys
from pathlib import Path

import pytest

from gobby.agents import droid_ps_shim
from gobby.agents.droid_ps_shim import droid_ps_shim_dir, ps_shim_env

pytestmark = pytest.mark.unit

# ``_stub_darwin`` patches ``subprocess.run`` to stand in for codesign, so the
# wrapper tests keep a handle on the real one to actually execute the script.
_REAL_SUBPROCESS_RUN = subprocess.run


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

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        Path(argv[-1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return source, calls


class TestPsShimEnv:
    def test_contributes_nothing_without_a_shim_directory(self) -> None:
        assert ps_shim_env(None, {"PATH": "/usr/bin"}) == {}

    def test_prepends_the_shim_directory_so_the_supervisor_resolves_it_first(self) -> None:
        assert ps_shim_env(Path("/shim"), {"PATH": "/usr/bin:/bin"})["PATH"] == (
            f"/shim{os.pathsep}/usr/bin:/bin"
        )

    def test_uses_the_shim_directory_alone_when_path_is_unset(self) -> None:
        assert ps_shim_env(Path("/shim"), {})["PATH"] == "/shim"

    def test_points_shell_at_the_wrapper_so_droids_harvest_keeps_the_shim(self) -> None:
        # Droid rebuilds its command environment from ``$SHELL -ilc``; PATH alone
        # is discarded by path_helper in that login shell.
        overrides = ps_shim_env(Path("/shim"), {"SHELL": "/bin/zsh"})
        assert overrides["SHELL"] == f"/shim{os.sep}gobby-droid-shell"
        assert overrides["GOBBY_DROID_REAL_SHELL"] == "/bin/zsh"
        assert overrides["GOBBY_DROID_PS_SHIM_DIR"] == "/shim"

    def test_omits_the_real_shell_when_the_environment_has_none(self) -> None:
        assert "GOBBY_DROID_REAL_SHELL" not in ps_shim_env(Path("/shim"), {})

    def test_never_records_its_own_wrapper_as_the_real_shell(self) -> None:
        # A resume re-prepares the launch from an already-rewritten environment;
        # adopting that SHELL would make the wrapper exec itself forever.
        assert "GOBBY_DROID_REAL_SHELL" not in ps_shim_env(
            Path("/shim"), {"SHELL": f"/shim{os.sep}gobby-droid-shell"}
        )

    def test_keeps_the_real_shell_recorded_by_an_earlier_launch(self) -> None:
        overrides = ps_shim_env(
            Path("/shim"),
            {"SHELL": f"/shim{os.sep}gobby-droid-shell", "GOBBY_DROID_REAL_SHELL": "/bin/zsh"},
        )
        assert overrides["GOBBY_DROID_REAL_SHELL"] == "/bin/zsh"


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
        assert shim.read_bytes() == b"signed"
        assert len(calls) == 1
        assert calls[0][0] == str(droid_ps_shim.CODESIGN)
        assert calls[0][1:4] == ["-f", "-s", "-"]
        assert calls[0][4].startswith(str(directory / ".ps."))

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

        monkeypatch.setattr(subprocess, "run", failing_run)
        # A broken shim must never break the spawn; Droid degrades to today's behavior.
        assert droid_ps_shim_dir() is None


def _materialized_shim_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _stub_darwin(monkeypatch, tmp_path)
    directory = droid_ps_shim_dir()
    assert directory is not None
    return directory


def _wrapper_env(directory: Path, real_shell: str) -> dict[str, str]:
    return {
        "GOBBY_DROID_REAL_SHELL": real_shell,
        "GOBBY_DROID_PS_SHIM_DIR": str(directory),
        "PATH": "/usr/bin:/bin",
    }


class TestShellWrapper:
    def test_is_materialized_executable_alongside_ps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        wrapper = _materialized_shim_dir(monkeypatch, tmp_path) / "gobby-droid-shell"
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o111

    def test_is_restored_when_a_cached_directory_lost_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = _materialized_shim_dir(monkeypatch, tmp_path)
        (directory / "gobby-droid-shell").unlink()
        # The cached-``ps`` fast path must not skip restoring the wrapper.
        assert droid_ps_shim_dir() == directory
        assert (directory / "gobby-droid-shell").is_file()


@pytest.mark.skipif(os.name != "posix", reason="the wrapper is a POSIX shell script")
def test_the_wrapper_reasserts_the_shim_on_the_command_operand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _materialized_shim_dir(monkeypatch, tmp_path)
    echo_argv = tmp_path / "echo-argv"
    echo_argv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    os.chmod(echo_argv, 0o755)  # nosec B103 # test fixture in a tmp_path.

    result = _REAL_SUBPROCESS_RUN(  # nosec B603 # fixed argv, locally built wrapper.
        [str(directory / "gobby-droid-shell"), "-ilc", "echo marker"],
        env=_wrapper_env(directory, str(echo_argv)),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    # The rewritten operand carries its own newline, so compare the whole stream.
    assert result.stdout == f"-ilc\nPATH={directory}:$PATH\necho marker\n", (
        "the real shell must keep Droid's flags and receive the re-asserted PATH"
    )


@pytest.mark.skipif(os.name != "posix", reason="the wrapper is a POSIX shell script")
def test_the_wrapper_leaves_an_interactive_invocation_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _materialized_shim_dir(monkeypatch, tmp_path)
    echo_argv = tmp_path / "echo-argv"
    echo_argv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    os.chmod(echo_argv, 0o755)  # nosec B103 # test fixture in a tmp_path.

    result = _REAL_SUBPROCESS_RUN(  # nosec B603 # fixed argv, locally built wrapper.
        [str(directory / "gobby-droid-shell"), "-i", "-l"],
        env=_wrapper_env(directory, str(echo_argv)),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-i", "-l"]


@pytest.mark.skipif(os.name != "posix", reason="the wrapper is a POSIX shell script")
def test_the_wrapper_survives_a_login_shell_rebuilding_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The regression this wrapper exists for: ``path_helper`` demotes the shim.

    A bare ``PATH`` prepend loses to ``/bin`` once a login shell has run, which is
    how Droid harvests the environment for every ``Execute``.
    """
    directory = _materialized_shim_dir(monkeypatch, tmp_path)
    wrapper = directory / "gobby-droid-shell"
    env = _wrapper_env(directory, "/bin/sh")

    unwrapped = _REAL_SUBPROCESS_RUN(  # nosec B603 # fixed argv, system shell.
        ["/bin/sh", "-lc", "command -v ps"],
        env={**env, "PATH": f"{directory}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    wrapped = _REAL_SUBPROCESS_RUN(  # nosec B603 # fixed argv, locally built wrapper.
        [str(wrapper), "-lc", "command -v ps"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert wrapped.returncode == 0, wrapped.stderr
    assert wrapped.stdout.strip() == str(directory / "ps")
    if unwrapped.stdout.strip() != str(directory / "ps"):
        assert wrapped.stdout.strip() != unwrapped.stdout.strip()


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
