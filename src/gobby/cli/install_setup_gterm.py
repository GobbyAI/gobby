"""gterm installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.cli.install_setup_versions import managed_version_satisfies_pin
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_models import compare_versions
from gobby.install.bin_freshness_promotion import stage_and_promote_binary_file
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.install.version_probe import probe_native_bin_version

GTERM_NO_ZIG_SKIP_REASON = (
    "zig not found on PATH; skipping local gterm workspace build (vt-engine requires Zig 0.15)"
)
_WORKSPACE_BUILD_TIMEOUT_SECONDS = 600
_CRATE_PACKAGE = "gobby-terminal"
_CRATE_DIR = "gterminal"


def get_latest_gterm_version(module: Any) -> str | None:
    """Query crates.io for latest gterm version."""
    try:
        req = module.Request(
            module._GTERM_CRATES_API,
            headers={"User-Agent": "gobby-installer/1.0"},
        )
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("gterm: could not check latest version: %s", e)
        return None


def get_installed_gterm_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gterm version, preferring the binary over the stamp."""
    gterm_path = bin_dir / module._GTERM_BIN_NAME
    if gterm_path.exists():
        probed_version = probe_gterm_version(module, gterm_path)
        if probed_version:
            return probed_version

    stamp = bin_dir / module._GTERM_VERSION_STAMP
    try:
        return stamp.read_text().strip() if stamp.exists() else None
    except OSError:
        return None


def probe_gterm_version(module: Any, gterm_path: Path) -> str | None:
    """Probe gterm binary for a version string."""
    return probe_native_bin_version(
        gterm_path,
        runner=module.subprocess.run,
        logger=module.logger,
        label="gterm",
    )


def write_gterm_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gterm version stamp atomically."""
    stamp = str(bin_dir / module._GTERM_VERSION_STAMP)
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gterm-version-",
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


def install_gterm_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract gterm from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GTERM_BIN_NAME,
            artifact_name="gterm",
            target=target,
            version=version,
            tag_prefix=module._GTERM_RELEASE_TAG_PREFIX,
            label="gterm",
        )
    )


def _zig_on_path(module: Any) -> bool:
    return module.shutil.which("zig") is not None


def install_gterm_from_submodule(module: Any, bin_dir: Path) -> bool:
    """Build gterm from the local Rust workspace when cargo and zig are available."""
    if not module.shutil.which("cargo"):
        return False

    search = Path(__file__).resolve().parent
    for _ in range(10):
        manifest = search / "Cargo.toml"
        crate_manifest = search / "crates" / _CRATE_DIR / "Cargo.toml"
        if manifest.exists() and crate_manifest.exists():
            break
        search = search.parent
    else:
        module.logger.debug(
            "gterm workspace not found after searching %d parents from %s",
            10,
            Path(__file__).resolve().parent,
        )
        return False

    if not _zig_on_path(module):
        module.click.echo(f"  {GTERM_NO_ZIG_SKIP_REASON}")
        module.logger.info("gterm: %s", GTERM_NO_ZIG_SKIP_REASON)
        return False

    try:
        module.click.echo(
            "  Building gterm from local workspace with --features vt-engine "
            "(first libghostty-vt build may take several minutes)..."
        )
        result = module.subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                _CRATE_PACKAGE,
                "--features",
                "vt-engine",
                "--manifest-path",
                str(manifest),
            ],
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_BUILD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return False

        release_dir = manifest.parent / "target" / "release"
        src_bin = release_dir / module._GTERM_BIN_NAME
        if not src_bin.exists():
            return False

        dest = bin_dir / module._GTERM_BIN_NAME
        lock = try_acquire_native_bin_lock("gterm", bin_dir=bin_dir)
        if lock is None:
            module.logger.warning("gterm: native binary update is already in progress")
            return False
        with lock:
            stage_and_promote_binary_file(src_bin, destination=dest)
        return True
    except (FileNotFoundError, module.subprocess.TimeoutExpired, OSError) as e:
        module.logger.warning("gterm: local workspace build failed: %s", e)
        return False


def install_gterm_from_cargo_git(module: Any, bin_dir: Path) -> bool:
    """Install gterm from source via cargo install --git."""
    if not module.shutil.which("cargo"):
        return False
    if not _zig_on_path(module):
        module.logger.info("gterm: %s", GTERM_NO_ZIG_SKIP_REASON)
        return False
    try:
        module.click.echo("  Compiling gterm from source (this may take several minutes)...")
        result = module.subprocess.run(
            [
                "cargo",
                "install",
                "--git",
                "https://github.com/GobbyAI/gobby",
                "-p",
                _CRATE_PACKAGE,
                "--features",
                "vt-engine",
                "--root",
                str(bin_dir.parent),
            ],
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_BUILD_TIMEOUT_SECONDS,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gterm: cargo install --git failed: %s", e)
        return False


def install_gterm_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install gterm via cargo-binstall."""
    if not module.shutil.which("cargo-binstall"):
        return False
    try:
        crate = f"{_CRATE_PACKAGE}@{version}" if version else _CRATE_PACKAGE
        result = module.subprocess.run(
            [
                "cargo-binstall",
                crate,
                "--install-path",
                str(bin_dir),
                "--no-confirm",
                "--no-symlinks",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gterm: cargo-binstall failed: %s", e)
        return False


def install_gterm_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install gterm from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    if not _zig_on_path(module):
        module.logger.info("gterm: %s", GTERM_NO_ZIG_SKIP_REASON)
        return False
    try:
        cmd = [
            "cargo",
            "install",
            _CRATE_PACKAGE,
            "--features",
            "vt-engine",
            "--root",
            str(bin_dir.parent),
        ]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling gterm from source (this may take several minutes)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_BUILD_TIMEOUT_SECONDS,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gterm: cargo install failed: %s", e)
        return False


def install_gterm(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade gterm with the gcode fallback chain (zig-gated local build)."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    gterm_path = bin_dir / module._GTERM_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._GTERM_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("gterm: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_gterm_version(bin_dir)
    pinned_version = MANAGED_BIN_VERSION_PINS["gterm"]
    if gterm_path.exists() and not force:
        if installed_version and managed_version_satisfies_pin("gterm", installed_version):
            module._write_gterm_version_stamp(bin_dir, installed_version)
            return {"installed": False, "skipped": True, "version": installed_version}

    target_version = pinned_version
    if compare_versions(installed_version, pinned_version) == 1:
        target_version = installed_version
    bin_dir.mkdir(parents=True, exist_ok=True)
    method = None

    if module._install_gterm_from_submodule(bin_dir):
        method = "workspace"
    elif module._install_gterm_from_github(bin_dir, target, target_version):
        method = "github"
    elif module._install_gterm_from_cargo_binstall(bin_dir, target_version):
        method = "cargo-binstall"
    elif module._install_gterm_from_cargo_install(bin_dir, target_version):
        method = "cargo-install"
    elif module._install_gterm_from_cargo_git(bin_dir):
        method = "cargo-git"
    else:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    gterm_path.chmod(0o755)

    resolved_version = probe_gterm_version(module, gterm_path) or target_version or "unknown"
    module._write_gterm_version_stamp(bin_dir, resolved_version)

    module._ensure_gobby_bin_on_path(bin_dir)

    is_upgrade = installed_version is not None and installed_version != resolved_version
    return {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": method,
    }
