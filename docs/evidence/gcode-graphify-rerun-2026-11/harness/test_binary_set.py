"""The binary-set copy is one set, by new inode, and refuses an incomplete source."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from binary_set import install_binary_set
from runtime_boundary import EvidenceError

from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name

MEMBERS = [native_bin_name(member) for member in SET_MEMBERS] + [IDENTITY_STAMP_NAME]


def _stage_source(source: Path) -> None:
    source.mkdir(parents=True)
    for name in MEMBERS:
        path = source / name
        path.write_bytes(f"bytes of {name}".encode())
        if name != IDENTITY_STAMP_NAME:
            path.chmod(0o755)


def test_copies_every_member_with_identical_bytes_and_returns_its_path(tmp_path: Path) -> None:
    source = tmp_path / "bin"
    dest = tmp_path / "gobby-home/bin"
    _stage_source(source)

    copied = install_binary_set(source, dest, MEMBERS)

    assert sorted(copied) == sorted(MEMBERS)
    for name in MEMBERS:
        assert (dest / name).read_bytes() == (source / name).read_bytes()
        assert copied[name] == dest / name
        assert (
            hashlib.sha256((dest / name).read_bytes()).hexdigest()
            == hashlib.sha256((source / name).read_bytes()).hexdigest()
        )


def test_binaries_stay_executable_and_the_stamp_is_a_plain_file(tmp_path: Path) -> None:
    source = tmp_path / "bin"
    dest = tmp_path / "gobby-home/bin"
    _stage_source(source)

    install_binary_set(source, dest, MEMBERS)

    for name in MEMBERS:
        if name != IDENTITY_STAMP_NAME:
            assert os.access(dest / name, os.X_OK)


def test_replaces_an_existing_member_by_new_inode(tmp_path: Path) -> None:
    source = tmp_path / "bin"
    dest = tmp_path / "gobby-home/bin"
    _stage_source(source)
    dest.mkdir(parents=True)
    old = dest / native_bin_name("gcode")
    old.write_bytes(b"stale")
    old_inode = old.stat().st_ino

    install_binary_set(source, dest, MEMBERS)

    assert (dest / native_bin_name("gcode")).stat().st_ino != old_inode
    assert (dest / native_bin_name("gcode")).read_bytes() == (
        source / native_bin_name("gcode")
    ).read_bytes()


def test_leaves_no_staging_file_behind(tmp_path: Path) -> None:
    source = tmp_path / "bin"
    dest = tmp_path / "gobby-home/bin"
    _stage_source(source)

    install_binary_set(source, dest, MEMBERS)

    assert sorted(path.name for path in dest.iterdir()) == sorted(MEMBERS)


def test_a_copy_that_fails_midway_leaves_no_staging_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bin"
    dest = tmp_path / "gobby-home/bin"
    _stage_source(source)
    real_copymode = shutil.copymode
    calls: list[Path] = []

    def copymode_interrupted_on_the_second_member(
        src: Path, dst: Path, *, follow_symlinks: bool = True
    ) -> None:
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("copy interrupted")
        real_copymode(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copymode", copymode_interrupted_on_the_second_member)

    with pytest.raises(OSError, match="copy interrupted"):
        install_binary_set(source, dest, MEMBERS)

    assert list(dest.glob(".*.staging")) == []


@pytest.mark.parametrize("missing", MEMBERS)
def test_refuses_an_incomplete_source_set_and_copies_nothing(tmp_path: Path, missing: str) -> None:
    source = tmp_path / "bin"
    dest = tmp_path / "gobby-home/bin"
    _stage_source(source)
    (source / missing).unlink()

    with pytest.raises(EvidenceError, match="incomplete binary set"):
        install_binary_set(source, dest, MEMBERS)

    assert not dest.exists()
