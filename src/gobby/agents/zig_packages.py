"""Zig package directories that back a fetch-disabled libghostty-vt build."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

ZIG_PACKAGES = "p"
_ZIG_TARBALL_SUFFIX = ".tar.gz"
_VENDORED_LIBGHOSTTY_VT = Path("crates") / "gterminal" / "vendor" / "libghostty-vt"


def vendored_libghostty_vt(checkout: Path) -> Path:
    return checkout / _VENDORED_LIBGHOSTTY_VT


def machine_zig_packages() -> Path:
    return Path.home() / ".cache" / "zig" / ZIG_PACKAGES


def _extract_zig_package(tarball: Path, dest: Path, *, staging_parent: Path) -> None:
    """Unpack one package tarball into dest through a staged sibling directory."""
    staging = Path(tempfile.mkdtemp(prefix=".zig-pkg-", dir=staging_parent))
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extractall(staging, filter="data")
        try:
            os.replace(staging / dest.name, dest)
        except OSError:
            # A concurrent run materialized the same package first.
            if not dest.is_dir():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def materialize_zig_packages(
    source: Path,
    cache_root: Path,
    vendored_zig_pkg: Path | None,
    *,
    report: Callable[[str], None],
) -> bool:
    """Make every machine Zig package usable as an extracted directory.

    A `zig build --system <dir>` run resolves packages by id with fetching
    disabled, so tarball-only entries must be unpacked (reusing the vendored
    zig-pkg extraction when the package id matches) before that directory can
    back a sandboxed build. Entries the machine cache already holds extracted
    are symlinked instead: Zig resolves a `--system` package through a symlink,
    and copying the cache costs a minute and half a gigabyte per run. Returns
    True only when every source package resolved to a usable directory.
    """
    packages = cache_root / ZIG_PACKAGES
    packages.mkdir(parents=True, exist_ok=True)
    entries = sorted(source.iterdir())
    if not entries:
        # An empty machine cache cannot back a fetch-disabled --system build.
        return False
    complete = True
    for entry in entries:
        pkgid = entry.name.removesuffix(_ZIG_TARBALL_SUFFIX)
        dest = packages / pkgid
        if dest.exists() or dest.is_symlink():
            continue
        try:
            if entry.is_dir():
                dest.symlink_to(entry, target_is_directory=True)
                continue
            if not entry.name.endswith(_ZIG_TARBALL_SUFFIX):
                report(f"zig package cache entry {entry} is not a directory or tarball")
                complete = False
                continue
            vendored_copy = vendored_zig_pkg / pkgid if vendored_zig_pkg is not None else None
            if vendored_copy is not None and vendored_copy.is_dir():
                dest.symlink_to(vendored_copy, target_is_directory=True)
                continue
            _extract_zig_package(entry, dest, staging_parent=cache_root)
        except (OSError, tarfile.TarError) as exc:
            report(f"zig package {pkgid} could not be materialized: {exc}")
            complete = False
    return complete
