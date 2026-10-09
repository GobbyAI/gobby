"""Hermetic pinned upstream bytes for fake SRT installations."""

import gzip
from pathlib import Path

from gobby.agents.srt_package_patch import HTTP_PROXY_PATH, MUX_PROXY_PATH, apply_srt_proxy_patch


def upstream_proxy_bytes(name: str = "http-proxy") -> bytes:
    fixture = Path(__file__).parent / f"fixtures/srt/{name}-0.0.76.js.gz"
    return gzip.decompress(fixture.read_bytes())


def write_srt_proxy_fixture(root: Path, *, patched: bool = True) -> Path:
    path = root / HTTP_PROXY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(upstream_proxy_bytes())
    (root / MUX_PROXY_PATH).write_bytes(upstream_proxy_bytes("mux-proxy"))
    if patched:
        apply_srt_proxy_patch(root)
    return path
