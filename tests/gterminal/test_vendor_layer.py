"""Vendor layer for libghostty-vt and portable-pty (plan 1.1 / #20263).

These tests drive the shipped helper script, `crates/gterminal/build.rs`, and
workspace Cargo patch. They do not reimplement the Zig triple map or invoke
`cargo -p gobby-terminal` (that package lands in 1.2).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GTERMINAL = REPO_ROOT / "crates" / "gterminal"
VENDOR = GTERMINAL / "vendor"
BUILD_RS = GTERMINAL / "build.rs"
HELPER = REPO_ROOT / "scripts" / "build_vendored_libghostty_vt.sh"
VENDOR_JSON = VENDOR / "libghostty-vt.vendor.json"
LIBGHOSTTY_PATCHES = VENDOR / "libghostty-vt.patches.md"
PORTABLE_PTY_PATCHES = VENDOR / "portable-pty.patches.md"
WORKSPACE_CARGO = REPO_ROOT / "Cargo.toml"

ZIG_TARGET_MAP = {
    "x86_64-unknown-linux-gnu": "x86_64-linux-gnu",
    "aarch64-unknown-linux-gnu": "aarch64-linux-gnu",
    "x86_64-unknown-linux-musl": "x86_64-linux-musl",
    "aarch64-unknown-linux-musl": "aarch64-linux-musl",
    "x86_64-apple-darwin": "x86_64-macos",
    "aarch64-apple-darwin": "aarch64-macos",
    "x86_64-pc-windows-msvc": "x86_64-windows-msvc",
    "aarch64-pc-windows-msvc": "aarch64-windows-msvc",
}

REQUIRED_ZIG = "0.15"


def _compile_build_rs(tmp_path: Path) -> Path:
    assert BUILD_RS.is_file(), f"missing shipped build.rs at {BUILD_RS}"
    dest = tmp_path / "gterminal-build-script"
    compiled = subprocess.run(
        [
            "rustc",
            "--edition",
            "2021",
            "--crate-name",
            "gterminal_build",
            str(BUILD_RS),
            "-o",
            str(dest),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    return dest


def _run_build_rs(binary: Path, *, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        "CARGO_MANIFEST_DIR": str(GTERMINAL),
        "TARGET": "aarch64-apple-darwin",
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RUST_BACKTRACE": "0",
    }
    env.update(extra_env)
    return subprocess.run(
        [str(binary)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_rust_zig_targets(source: str) -> dict[str, str]:
    return dict(
        re.findall(
            r'"([a-z0-9_]+-[a-z0-9_]+-[a-z0-9_]+(?:-[a-z0-9_]+)?)"\s*=>\s*"([^"]+)"',
            source,
        )
    )


def _parse_shell_zig_targets(source: str) -> dict[str, str]:
    return dict(
        re.findall(
            r"([a-z0-9_]+-[a-z0-9_]+-[a-z0-9_]+(?:-[a-z0-9_]+)?)\s*\)\s*"
            r"echo\s+([a-z0-9_]+-[a-z0-9_]+(?:-[a-z0-9_]+)?)",
            source,
        )
    )


def _run_helper(
    *, extra_env: dict[str, str], timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    assert HELPER.is_file(), f"missing helper script at {HELPER}"
    env = os.environ.copy()
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(HELPER)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_build_rs_is_noop_without_vt_engine(tmp_path: Path) -> None:
    probe = tmp_path / "zig-probe"
    marker = tmp_path / "zig-invoked"
    probe.write_text(f"#!/bin/sh\necho invoked > '{marker}'\nexit 42\n", encoding="utf-8")
    probe.chmod(0o755)

    result = _run_build_rs(_compile_build_rs(tmp_path), extra_env={"ZIG": str(probe)})

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "build.rs invoked Zig without CARGO_FEATURE_VT_ENGINE"


def test_build_rs_rejects_unsupported_target_triple(tmp_path: Path) -> None:
    result = _run_build_rs(
        _compile_build_rs(tmp_path),
        extra_env={
            "CARGO_FEATURE_VT_ENGINE": "1",
            "TARGET": "powerpc-unknown-linux-gnu",
            "ZIG": str(tmp_path / "missing-zig"),
        },
    )

    combined = result.stderr + result.stdout
    assert result.returncode != 0
    assert "unsupported target" in combined
    assert "powerpc-unknown-linux-gnu" in combined


def test_build_rs_names_zig_version_when_binary_missing(tmp_path: Path) -> None:
    result = _run_build_rs(
        _compile_build_rs(tmp_path),
        extra_env={
            "CARGO_FEATURE_VT_ENGINE": "1",
            "ZIG": str(tmp_path / "missing-zig"),
        },
    )

    combined = result.stderr + result.stdout
    assert result.returncode != 0
    assert REQUIRED_ZIG in combined


def test_helper_fails_descriptively_when_zig_is_missing(tmp_path: Path) -> None:
    result = _run_helper(extra_env={"ZIG": str(tmp_path / "missing-zig")})

    combined = result.stderr + result.stdout
    assert result.returncode != 0
    assert REQUIRED_ZIG in combined


def test_helper_and_build_rs_share_zig_version_and_triple_map() -> None:
    build_src = BUILD_RS.read_text(encoding="utf-8")
    helper_src = HELPER.read_text(encoding="utf-8")

    assert _parse_rust_zig_targets(build_src) == ZIG_TARGET_MAP
    assert _parse_shell_zig_targets(helper_src) == ZIG_TARGET_MAP
    assert REQUIRED_ZIG in build_src
    assert REQUIRED_ZIG in helper_src
    assert "CARGO_FEATURE_VT_ENGINE" in build_src
    assert "GTERM_BUILD_" in build_src
    assert "HERDR_BUILD_" not in build_src


def test_workspace_patch_serves_vendored_portable_pty() -> None:
    cargo = tomllib.loads(WORKSPACE_CARGO.read_text(encoding="utf-8"))
    patch = cargo["patch"]["crates-io"]["portable-pty"]["path"]
    members = cargo.get("workspace", {}).get("members", [])
    vendored = (REPO_ROOT / patch / "Cargo.toml").resolve()

    assert patch == "crates/gterminal/vendor/portable-pty"
    assert vendored.is_file()
    assert "crates/gterminal" in members


def test_vendor_json_pins_ghostty_commit() -> None:
    meta = json.loads(VENDOR_JSON.read_text(encoding="utf-8"))
    assert meta["source_commit"].startswith("c5a21edf")
    assert "dist_archive" in meta
    assert "extracted_dir" in meta
    assert (VENDOR / "libghostty-vt" / "build.zig").is_file()
    assert (VENDOR / "libghostty-vt" / "src" / "lib_vt.zig").is_file()


def test_patch_provenance_records_base_commit_list_rationale_and_removal() -> None:
    for index, patch_dir in (
        (LIBGHOSTTY_PATCHES, VENDOR / "patches" / "libghostty-vt"),
        (PORTABLE_PTY_PATCHES, VENDOR / "patches" / "portable-pty"),
    ):
        text = index.read_text(encoding="utf-8").lower()
        assert "vendored base:" in text
        assert "reason:" in text
        assert "remove when:" in text
        patches = sorted(patch_dir.glob("*.patch"))
        assert patches, f"expected patches under {patch_dir}"
        listed = index.read_text(encoding="utf-8")
        missing = [path.name for path in patches if path.name not in listed]
        assert missing == []


def test_vendor_patches_are_applied_to_copied_trees() -> None:
    patches = sorted((VENDOR / "patches").rglob("*.patch"))
    assert patches, f"expected applied patches under {VENDOR / 'patches'}"
    for patch in patches:
        relative = patch.relative_to(GTERMINAL)
        checked = subprocess.run(
            ["git", "apply", "--check", "--reverse", str(relative)],
            cwd=GTERMINAL,
            capture_output=True,
            text=True,
            check=False,
        )
        assert checked.returncode == 0, (
            f"{relative} is not applied to the vendored tree:\n{checked.stdout}\n{checked.stderr}"
        )


@pytest.mark.slow
def test_helper_builds_vendored_libghostty_vt() -> None:
    zig = os.environ.get("ZIG") or shutil.which("zig")
    assert zig, "Zig 0.15 is required to build the vendored VT tree (set ZIG or PATH)"
    result = _run_helper(extra_env={"ZIG": zig}, timeout=600)
    assert result.returncode == 0, result.stderr + result.stdout
    lib_dir = VENDOR / "libghostty-vt" / "zig-out" / "lib"
    built = list(lib_dir.glob("*ghostty-vt*"))
    assert built, f"helper exited 0 but produced no libghostty-vt artifact under {lib_dir}"
