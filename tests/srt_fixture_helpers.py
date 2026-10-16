"""Hermetic pinned upstream bytes for fake SRT installations."""

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from gobby.agents.srt_package_patch import (
    HTTP_PROXY_PATH,
    MACOS_PROFILE_PATH,
    MUX_PROXY_PATH,
    apply_srt_proxy_patch,
)


def upstream_proxy_bytes(name: str = "http-proxy") -> bytes:
    fixture = Path(__file__).parent / f"fixtures/srt/{name}-0.0.79.js.gz"
    return gzip.decompress(fixture.read_bytes())


def write_srt_settings_fixture(root: Path) -> None:
    """Materialize the real settings parser, dependencies and manager without launching SRT."""
    fixture = Path(__file__).parent / "fixtures/srt/settings-loader-0.0.79.tar.gz"
    payload = fixture.read_bytes()
    expected = "c8b6152f8514389aa667948df67913edba8bf0592df839ed553a7fd1f5aa2925"
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("Pinned SRT settings loader fixture checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive.extractall(root, filter="data")


def write_srt_proxy_fixture(root: Path, *, patched: bool = True) -> Path:
    path = root / HTTP_PROXY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(upstream_proxy_bytes())
    (root / MUX_PROXY_PATH).write_bytes(upstream_proxy_bytes("mux-proxy"))
    (root / MACOS_PROFILE_PATH).write_bytes(upstream_proxy_bytes("macos-sandbox-utils"))
    if patched:
        apply_srt_proxy_patch(root)
    return path


def write_srt_parser_fixture(root: Path) -> Path:
    """Materialize exact parser dependencies from the integrity-verified 0.0.79 tarball."""
    fixture = Path(__file__).parent / "fixtures/srt/parser-dependencies-0.0.79.tar.gz"
    payload = fixture.read_bytes()
    expected = "4f897ae5ece67f3a6f7103e3553348a7c582aa10d14b11ea5d89f29ac548e282"
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("Pinned SRT parser dependency fixture checksum mismatch")
    package = root / HTTP_PROXY_PATH.parents[2]
    package.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive.extractall(package, filter="data")
    write_srt_proxy_fixture(root)
    return root / MACOS_PROFILE_PATH
