"""Integrity boundaries for the temporary upstream CONNECT drain fix."""

from pathlib import Path

import pytest

from gobby.agents.srt_package_patch import apply_srt_proxy_patch, verify_srt_proxy_patch
from tests.srt_fixture_helpers import write_srt_proxy_fixture


def test_patch_drains_only_the_two_upstream_close_handlers(tmp_path: Path) -> None:
    from gobby.agents.srt_package_patch import MUX_PROXY_PATH

    proxy = write_srt_proxy_fixture(tmp_path, patched=False)
    original = proxy.read_bytes()
    mux = tmp_path / MUX_PROXY_PATH
    original_mux = mux.read_bytes()

    apply_srt_proxy_patch(tmp_path)

    assert proxy.read_bytes() == original.replace(
        b"upstream.on('close', () => socket.destroy());",
        b"upstream.on('close', () => socket.end());",
    )
    assert mux.read_bytes() == original_mux.replace(
        b"upstream.once('close', () => client.destroy());",
        b"upstream.once('close', () => client.end());",
    )
    verify_srt_proxy_patch(tmp_path)
    apply_srt_proxy_patch(tmp_path)
    verify_srt_proxy_patch(tmp_path)


@pytest.mark.parametrize("name", ["http-proxy.js", "mux-proxy.js"])
def test_patch_refuses_unknown_upstream_bytes_without_writing(tmp_path: Path, name: str) -> None:
    http = write_srt_proxy_fixture(tmp_path, patched=False)
    proxy = http.with_name(name)
    original_http = http.read_bytes()
    corrupted = proxy.read_bytes() + b"// unexpected upstream drift\n"
    proxy.write_bytes(corrupted)

    with pytest.raises(ValueError, match="pinned upstream source"):
        apply_srt_proxy_patch(tmp_path)

    assert proxy.read_bytes() == corrupted
    if name == "mux-proxy.js":
        assert http.read_bytes() == original_http


@pytest.mark.parametrize("name", ["http-proxy.js", "mux-proxy.js"])
def test_verifier_rejects_unpatched_and_modified_proxy(tmp_path: Path, name: str) -> None:
    proxy = write_srt_proxy_fixture(tmp_path, patched=False).with_name(name)
    apply_srt_proxy_patch(tmp_path)
    from tests.srt_fixture_helpers import upstream_proxy_bytes

    proxy.write_bytes(upstream_proxy_bytes(name.removesuffix(".js")))
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_srt_proxy_patch(tmp_path)

    apply_srt_proxy_patch(tmp_path)
    proxy.write_bytes(proxy.read_bytes() + b"// tampered\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_srt_proxy_patch(tmp_path)
