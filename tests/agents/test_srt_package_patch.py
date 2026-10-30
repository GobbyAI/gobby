"""Integrity boundaries and behavior of the pinned upstream SRT fixes."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gobby.agents.srt_package_patch import apply_srt_proxy_patch, verify_srt_proxy_patch
from tests.srt_fixture_helpers import write_srt_proxy_fixture


def test_patch_drains_mux_and_verifies_all_runtime_bytes(tmp_path: Path) -> None:
    from gobby.agents.srt_package_patch import MUX_PROXY_PATH

    write_srt_proxy_fixture(tmp_path, patched=False)
    mux = tmp_path / MUX_PROXY_PATH
    original_mux = mux.read_bytes()

    apply_srt_proxy_patch(tmp_path)

    assert mux.read_bytes() == original_mux.replace(
        b"upstream.once('close', () => client.destroy());",
        b"upstream.once('close', () => client.end());",
    )
    verify_srt_proxy_patch(tmp_path)
    apply_srt_proxy_patch(tmp_path)
    verify_srt_proxy_patch(tmp_path)


@pytest.mark.parametrize("name", ["http-proxy.js", "mux-proxy.js", "macos-sandbox-utils.js"])
def test_patch_refuses_unknown_upstream_bytes_without_writing(tmp_path: Path, name: str) -> None:
    http = write_srt_proxy_fixture(tmp_path, patched=False)
    proxy = http.with_name(name)
    corrupted = proxy.read_bytes() + b"// unexpected upstream drift\n"
    proxy.write_bytes(corrupted)
    originals = {path: path.read_bytes() for path in http.parent.glob("*.js")}

    with pytest.raises(ValueError, match="pinned upstream source"):
        apply_srt_proxy_patch(tmp_path)

    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("name", ["http-proxy.js", "mux-proxy.js", "macos-sandbox-utils.js"])
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


def test_connect_failure_reporter_writes_only_to_the_journal(tmp_path: Path) -> None:
    # The proxy shares the sandboxed agent's terminal; console output lands in its composer.
    proxy = write_srt_proxy_fixture(tmp_path).read_bytes()
    start = proxy.index(b"function reportConnectFailure(")
    reporter = proxy[start : proxy.index(b"export function createHttpProxyServer(", start)]

    assert b"globalThis.gobbySrtProxyFailure?.(record);" in reporter
    assert b"console." not in reporter
    assert b"process.std" not in reporter
    assert b"[gobby-srt proxy]" not in proxy


_WHICH_PROBE = """
import {pathToFileURL} from 'node:url';
const {whichSync} = await import(pathToFileURL(process.argv[1]));
console.log(JSON.stringify(whichSync(process.argv[2])));
"""


def _run_which(node: str, which_js: Path, search_path: str, name: str) -> str | None:
    result = subprocess.run(
        [node, "--input-type=module", "-e", _WHICH_PROBE, str(which_js), name],
        env={"PATH": search_path},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved is None or isinstance(resolved, str)
    return resolved


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("which_mode", ["absent", "exceeds-bound"])
def test_patched_which_resolves_shell_when_which_cannot_answer(
    tmp_path: Path, which_mode: str
) -> None:
    """Regression for #24633: SRT's wrap reported bash missing from a loaded host."""
    from gobby.agents.srt_package_patch import WHICH_PATH
    from tests.srt_fixture_helpers import upstream_proxy_bytes

    node = shutil.which("node")
    assert node is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "bash", "exit 0")
    if which_mode == "exceeds-bound":
        # Answers correctly, but only after upstream's 1s spawnSync bound.
        _write_executable(bin_dir / "which", f'/bin/sleep 3; echo "{bin_dir}/$1"')
    upstream = tmp_path / "upstream-which.js"
    upstream.write_bytes(upstream_proxy_bytes("which"))
    package = tmp_path / "package"
    write_srt_proxy_fixture(package)

    assert _run_which(node, upstream, str(bin_dir), "bash") is None
    patched = _run_which(node, package / WHICH_PATH, str(bin_dir), "bash")
    assert patched == str(bin_dir / "bash")


def test_patched_which_skips_non_executable_entries_like_which(tmp_path: Path) -> None:
    from gobby.agents.srt_package_patch import WHICH_PATH

    node = shutil.which("node")
    assert node is not None
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "bash").write_text("not executable\n", encoding="utf-8")
    (first / "bash").chmod(0o644)
    (first / "zsh").mkdir()
    _write_executable(second / "bash", "exit 0")
    _write_executable(second / "zsh", "exit 0")
    search_path = os.pathsep.join((str(first), str(second)))
    write_srt_proxy_fixture(tmp_path / "package")
    which_js = tmp_path / "package" / WHICH_PATH

    assert _run_which(node, which_js, search_path, "bash") == str(second / "bash")
    assert _run_which(node, which_js, search_path, "zsh") == str(second / "zsh")
    assert _run_which(node, which_js, search_path, "missing-shell") is None
    absolute = str(second / "bash")
    assert _run_which(node, which_js, search_path, absolute) == absolute
