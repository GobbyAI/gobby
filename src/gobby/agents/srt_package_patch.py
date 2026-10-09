"""Pinned CONNECT drain fix, removable when an upstream release includes it."""

import hashlib
from pathlib import Path

from gobby.utils.dependency_requirements import SRT_RELEASE

HTTP_PROXY_PATH = Path("node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/http-proxy.js")
MUX_PROXY_PATH = HTTP_PROXY_PATH.with_name("mux-proxy.js")
_PATCHES = (
    (
        HTTP_PROXY_PATH,
        "fbf1184b57d41400f5c73b5f0626df901caae0278b7b37103263a587e9c0cda4",
        SRT_RELEASE.http_proxy_sha256,
        b"upstream.on('close', () => socket.destroy());",
    ),
    (
        MUX_PROXY_PATH,
        "07d6eb4bab65e9abcf6e52f45216903cc82cba890139a149a771733b6f50088a",
        SRT_RELEASE.mux_proxy_sha256,
        b"upstream.once('close', () => client.destroy());",
    ),
)


def verify_srt_proxy_patch(root: Path) -> None:
    """Require the exact patched bytes, independently of the writable manifest."""
    for relative_path, _, expected, _ in _PATCHES:
        if hashlib.sha256((root / relative_path).read_bytes()).hexdigest() != expected:
            raise ValueError(
                f"managed SRT {relative_path.name} checksum mismatch; run `gobby install srt`"
            )


def apply_srt_proxy_patch(root: Path) -> None:
    """Patch only the verified upstream file in an unpublished staging tree."""
    patches: list[tuple[Path, bytes]] = []
    for relative_path, original_sha256, expected, destroy in _PATCHES:
        path = root / relative_path
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest == expected:
            continue
        if digest != original_sha256 or original.count(destroy) != 1:
            raise ValueError(
                f"managed SRT {relative_path.name} does not match the pinned upstream source"
            )
        patched = original.replace(destroy, destroy.replace(b"destroy()", b"end()"))
        if hashlib.sha256(patched).hexdigest() != expected:
            raise ValueError(f"managed SRT {relative_path.name} patch checksum mismatch")
        patches.append((path, patched))
    for path, patched in patches:
        path.write_bytes(patched)
