"""gmcp installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from gobby.agents.cargo_target import cargo_release_dir
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_promotion import (
    clear_source_hash,
    file_sha256,
    stage_and_promote_binary_file,
    workspace_binary_is_current,
    write_source_hash,
)
from gobby.install.bin_set_coherence import _codesign_workspace_binary
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.install.version_probe import probe_native_bin_version
from gobby.paths import get_gobby_home

from .install_setup_gterm import ManagedBinaryReleaseMissing

_CRATE_PACKAGE = "gobby-mcp"
_CRATE_DIR = "gmcp"
_VERSION_STAMP = ".gmcp-version"
_DAEMON_START_FILE = "gmcp-daemon-start.json"
_WORKSPACE_BUILD_TIMEOUT_SECONDS = 1800


def probe_gmcp_version(module: Any, gmcp_path: Path) -> str | None:
    """Probe gmcp binary for a version string."""
    return probe_native_bin_version(
        gmcp_path,
        runner=module.subprocess.run,
        logger=module.logger,
        label="gmcp",
    )


def get_installed_gmcp_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gmcp version, preferring the binary over the stamp."""
    gmcp_path = bin_dir / module._GMCP_BIN_NAME
    if gmcp_path.exists():
        probed_version = probe_gmcp_version(module, gmcp_path)
        if probed_version:
            return probed_version

    stamp = bin_dir / _VERSION_STAMP
    try:
        return stamp.read_text().strip() if stamp.exists() else None
    except OSError:
        return None


def write_gmcp_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gmcp version stamp atomically."""
    stamp = str(bin_dir / _VERSION_STAMP)
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gmcp-version-",
        suffix=".tmp",
    )
    try:
        with module.os.fdopen(fd, "w") as f:
            f.write(version + "\n")
            f.flush()
            module.os.fsync(f.fileno())
        module.os.replace(tmp_path, stamp)
    except Exception:
        if module.os.path.exists(tmp_path):
            module.os.unlink(tmp_path)
        raise


def install_gmcp_from_submodule(module: Any, bin_dir: Path) -> str | None:
    """Build gmcp from the local Rust workspace when available (Zig-free)."""
    if not module.shutil.which("cargo"):
        return None

    search = Path(__file__).resolve().parent
    for _ in range(10):
        manifest = search / "Cargo.toml"
        crate_manifest = search / "crates" / _CRATE_DIR / "Cargo.toml"
        if manifest.exists() and crate_manifest.exists():
            break
        search = search.parent
    else:
        module.logger.debug(
            "gmcp workspace not found after searching %d parents from %s",
            10,
            Path(__file__).resolve().parent,
        )
        return None

    try:
        module.click.echo("  Building gmcp from local workspace (this may take several minutes)...")
        result = module.subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                _CRATE_PACKAGE,
                "--manifest-path",
                str(manifest),
            ],
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_BUILD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None

        release_dir = cargo_release_dir(manifest.parent)
        src_bin = release_dir / module._GMCP_BIN_NAME
        if not src_bin.exists():
            return None
        return promote_gmcp_workspace_artifact(module, src_bin, bin_dir, manifest.parent)
    except (FileNotFoundError, module.subprocess.TimeoutExpired, OSError) as e:
        module.logger.warning("gmcp: local workspace build failed: %s", e)
        return None


def promote_gmcp_workspace_artifact(
    module: Any, src_bin: Path, bin_dir: Path, workspace_root: Path
) -> str | None:
    """Promote a built gmcp through a signed new inode; None when another update holds it."""
    dest = bin_dir / module._GMCP_BIN_NAME
    if workspace_binary_is_current(bin_dir, "gmcp", src_bin, dest, workspace_root=workspace_root):
        return "current"
    lock = try_acquire_native_bin_lock("gmcp", bin_dir=bin_dir)
    if lock is None:
        module.logger.warning("gmcp: native binary update is already in progress")
        return None
    with lock:
        stage_and_promote_binary_file(
            src_bin,
            destination=dest,
            prepare_staged=_codesign_workspace_binary,
            workspace_root=workspace_root,
        )
        write_source_hash(bin_dir, "gmcp", file_sha256(src_bin))
    return "promoted"


def daemon_start_argv() -> list[str]:
    """The command the gmcp wrapper runs to start the local daemon."""
    return [sys.executable, "-m", "gobby.cli", "start"]


def write_daemon_start_command(gobby_home: Path, argv: list[str]) -> None:
    """Replace the gmcp daemon start command atomically; mode 0644 because it holds no secret."""
    target = gobby_home / _DAEMON_START_FILE
    fd, tmp_path = tempfile.mkstemp(
        dir=str(gobby_home), prefix=".gmcp-daemon-start-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(json.dumps(argv))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def install_gmcp(module: Any, force: bool = False) -> dict[str, Any]:
    """Build and promote gmcp from the checkout; outside one, an absent binary raises."""
    bin_dir = get_gobby_home() / "bin"
    gmcp_path = bin_dir / module._GMCP_BIN_NAME
    installed_version = module._get_installed_gmcp_version(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    if force:
        clear_source_hash(bin_dir, "gmcp")

    outcome = module._install_gmcp_from_submodule(bin_dir)
    if not outcome:
        if force or not gmcp_path.exists():
            raise ManagedBinaryReleaseMissing("gmcp")
        local_version = installed_version or "unknown"
        module._write_gmcp_version_stamp(bin_dir, local_version)
        return {
            "installed": False,
            "skipped": True,
            "version": local_version,
            "method": "local",
            "reason": "unverified: local build unavailable",
        }
    if outcome == "current":
        local_version = installed_version or "unknown"
        module._write_gmcp_version_stamp(bin_dir, local_version)
        return {"installed": False, "skipped": True, "version": local_version, "method": "local"}

    gmcp_path.chmod(0o755)
    version = probe_gmcp_version(module, gmcp_path) or MANAGED_BIN_VERSION_PINS["gmcp"]
    module._write_gmcp_version_stamp(bin_dir, version)
    write_daemon_start_command(get_gobby_home(), daemon_start_argv())
    return {
        "installed": True,
        "upgraded": installed_version is not None and installed_version != version,
        "version": version,
        "method": "workspace",
    }
