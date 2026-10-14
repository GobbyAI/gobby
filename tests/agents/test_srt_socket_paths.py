"""Private SRT socket roots stay bounded and cannot be granted to providers."""

import os
import stat
from pathlib import Path

import pytest

from gobby.agents import sandbox, sandbox_policy, srt_socket_paths
from gobby.agents.sandbox import SandboxConfig

pytestmark = pytest.mark.unit


def test_short_home_keeps_existing_socket_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srt_socket_paths, "get_gobby_home", lambda: Path("/gobby"))
    assert srt_socket_paths.srt_socket_directory() == Path("/gobby/runtime/srt-sock")


def test_long_lexical_alias_uses_canonical_short_socket_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / ("long-alias-" * 12)
    alias.symlink_to("/gobby", target_is_directory=True)
    monkeypatch.setattr(srt_socket_paths, "get_gobby_home", lambda: alias)
    assert srt_socket_paths.srt_socket_directory() == Path("/gobby/runtime/srt-sock")


def test_long_home_uses_bounded_stable_home_specific_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ("deep-home-" * 12)
    monkeypatch.setattr(srt_socket_paths, "get_gobby_home", lambda: home)
    directory = srt_socket_paths.srt_socket_directory()
    _, private_root = srt_socket_paths.private_srt_socket_roots()
    assert directory.parent == private_root.resolve()
    assert len(os.fsencode(directory)) + 40 < 104
    assert len(os.fsencode(directory / "srt-mux-4294967295-zzzzzzzzzzz.sock")) + 1 <= 104
    assert directory == srt_socket_paths.srt_socket_directory()
    monkeypatch.setattr(srt_socket_paths, "get_gobby_home", lambda: home / "another")
    assert directory != srt_socket_paths.srt_socket_directory()


def test_socket_directory_is_created_private_and_tightens_existing_permissions(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "sockets"
    srt_socket_paths._ensure_private_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    directory.chmod(0o755)
    srt_socket_paths._ensure_private_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("entry", ["symlink", "file"])
def test_socket_directory_refuses_unsafe_entry_without_changing_target(
    tmp_path: Path, entry: str
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    before = target.stat().st_mode
    directory = tmp_path / "sockets"
    if entry == "symlink":
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.write_text("not a directory")
    with pytest.raises(OSError):
        srt_socket_paths._ensure_private_directory(directory)
    assert target.stat().st_mode == before


def test_socket_directory_refuses_foreign_owner_before_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "sockets"
    directory.mkdir(mode=0o755)
    before = directory.stat().st_mode
    foreign = os.stat_result((before, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0))
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fstat", lambda _fd: foreign)
        with pytest.raises(PermissionError, match="Unsafe SRT socket directory"):
            srt_socket_paths._ensure_private_directory(directory)
    assert directory.stat().st_mode == before


@pytest.mark.parametrize("fallback", [False, True])
def test_mux_directory_refuses_preexisting_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    home_root = tmp_path / "home-sockets"
    short_root = tmp_path / "fallback-sockets"
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    before = target.stat().st_mode
    (short_root if fallback else home_root).symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        srt_socket_paths, "private_srt_socket_roots", lambda _home=None: (home_root, short_root)
    )
    monkeypatch.setattr(srt_socket_paths, "_SOCKET_DIRECTORY_BUDGET", 0 if fallback else 4096)
    with pytest.raises(OSError):
        srt_socket_paths.srt_mux_tmpdir()
    assert target.stat().st_mode == before
    assert list(target.iterdir()) == []


def test_fallback_parent_and_other_homes_remain_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / ("deep-home-" * 12)))
    _, private_root = srt_socket_paths.private_srt_socket_roots()
    assert str(private_root) in sandbox_policy.sensitive_roots()
    assert str(private_root) in sandbox_policy.sensitive_write_roots()
    for grant in (private_root, private_root.parent, private_root / "another-home" / "socket"):
        with pytest.raises(ValueError, match="sensitive root"):
            sandbox_policy.assert_sensitive_path_contract([str(grant)])
    sandbox_policy.assert_sensitive_path_contract([str(tmp_path / "own-run-temp")])


@pytest.mark.parametrize("home,fallback", [("a" * 45, False), ("a" * 46, True), ("é" * 24, True)])
def test_socket_budget_counts_encoded_bytes(
    monkeypatch: pytest.MonkeyPatch, home: str, fallback: bool
) -> None:
    monkeypatch.setattr(srt_socket_paths, "get_gobby_home", lambda: Path("/") / home)
    directory = srt_socket_paths.srt_socket_directory()
    home_root, short_root = srt_socket_paths.private_srt_socket_roots()
    assert directory == (short_root.resolve() / directory.name if fallback else home_root)
    assert len(os.fsencode(directory)) + 40 < 104


def test_mux_directory_secures_fallback_parent_and_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private-sockets"
    directory = parent / "home-key"
    monkeypatch.setattr(
        srt_socket_paths, "private_srt_socket_roots", lambda _home=None: (tmp_path, parent)
    )
    monkeypatch.setattr(srt_socket_paths, "srt_socket_directory", lambda: directory)
    assert srt_socket_paths.srt_mux_tmpdir() == directory
    for path in (parent, directory):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.stat().st_uid == os.getuid()


async def test_provider_cannot_grant_socket_access_to_private_monitor_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_git_metadata(_workspace: Path) -> list[str]:
        return []

    monkeypatch.setattr(sandbox, "_git_metadata_write_paths", no_git_metadata)
    _, private_root = srt_socket_paths.private_srt_socket_roots()
    config = SandboxConfig(
        enabled=True,
        backend="srt",
        allow_unix_sockets=[str(private_root / "other-home" / "socket")],
    )
    with pytest.raises(ValueError, match="sensitive root"):
        await sandbox.compute_sandbox_paths(config, str(tmp_path))
