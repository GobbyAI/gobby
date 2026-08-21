"""gclient installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.cli.install_setup_versions import managed_version_satisfies_pin
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_models import compare_versions
from gobby.install.bin_freshness_promotion import stage_and_promote_binary_file
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.install.version_probe import probe_native_bin_version

_CRATE_PACKAGE = "gobby-client"
_CRATE_DIR = "gclient"


def get_latest_gclient_version(module: Any) -> str | None:
    """Query crates.io for latest gclient version."""
    try:
        req = module.Request(
            module._GCLIENT_CRATES_API,
            headers={"User-Agent": "gobby-installer/1.0"},
        )
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("gclient: could not check latest version: %s", e)
        return None


def get_installed_gclient_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gclient version, preferring the binary over the stamp."""
    gclient_path = bin_dir / module._GCLIENT_BIN_NAME
    if gclient_path.exists():
        probed_version = probe_gclient_version(module, gclient_path)
        if probed_version:
            return probed_version

    stamp = bin_dir / module._GCLIENT_VERSION_STAMP
    try:
        return stamp.read_text().strip() if stamp.exists() else None
    except OSError:
        return None


def probe_gclient_version(module: Any, gclient_path: Path) -> str | None:
    """Probe gclient binary for a version string."""
    return probe_native_bin_version(
        gclient_path,
        runner=module.subprocess.run,
        logger=module.logger,
        label="gclient",
    )


def write_gclient_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gclient version stamp atomically."""
    stamp = str(bin_dir / module._GCLIENT_VERSION_STAMP)
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gclient-version-",
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


def install_gclient_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract gclient from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GCLIENT_BIN_NAME,
            artifact_name="gclient",
            target=target,
            version=version,
            tag_prefix=module._GCLIENT_RELEASE_TAG_PREFIX,
            label="gclient",
        )
    )


def install_gclient_from_submodule(module: Any, bin_dir: Path) -> bool:
    """Build gclient from the local Rust workspace when available (Zig-free)."""
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
            "gclient workspace not found after searching %d parents from %s",
            10,
            Path(__file__).resolve().parent,
        )
        return False

    try:
        module.click.echo(
            "  Building gclient from local workspace (this may take 30-60 seconds)..."
        )
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
            timeout=180,
        )
        if result.returncode != 0:
            return False

        release_dir = manifest.parent / "target" / "release"
        src_bin = release_dir / module._GCLIENT_BIN_NAME
        if not src_bin.exists():
            return False

        dest = bin_dir / module._GCLIENT_BIN_NAME
        lock = try_acquire_native_bin_lock("gclient", bin_dir=bin_dir)
        if lock is None:
            module.logger.warning("gclient: native binary update is already in progress")
            return False
        with lock:
            stage_and_promote_binary_file(src_bin, destination=dest)
        return True
    except (FileNotFoundError, module.subprocess.TimeoutExpired, OSError) as e:
        module.logger.warning("gclient: local workspace build failed: %s", e)
        return False


def install_gclient_from_cargo_git(module: Any, bin_dir: Path) -> bool:
    """Install gclient from source via cargo install --git."""
    if not module.shutil.which("cargo"):
        return False
    try:
        module.click.echo("  Compiling gclient from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            [
                "cargo",
                "install",
                "--git",
                "https://github.com/GobbyAI/gobby",
                "-p",
                _CRATE_PACKAGE,
                "--root",
                str(bin_dir.parent),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gclient: cargo install --git failed: %s", e)
        return False


def install_gclient_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install gclient via cargo-binstall."""
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
        module.logger.warning("gclient: cargo-binstall failed: %s", e)
        return False


def install_gclient_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install gclient from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    try:
        cmd = ["cargo", "install", _CRATE_PACKAGE, "--root", str(bin_dir.parent)]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling gclient from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gclient: cargo install failed: %s", e)
        return False


def install_gclient(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade gclient with the gcode fallback chain (Zig-free)."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    gclient_path = bin_dir / module._GCLIENT_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._GCLIENT_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("gclient: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_gclient_version(bin_dir)
    pinned_version = MANAGED_BIN_VERSION_PINS["gclient"]
    if gclient_path.exists() and not force:
        if installed_version and managed_version_satisfies_pin("gclient", installed_version):
            module._write_gclient_version_stamp(bin_dir, installed_version)
            return {"installed": False, "skipped": True, "version": installed_version}

    target_version = pinned_version
    if compare_versions(installed_version, pinned_version) == 1:
        target_version = installed_version
    bin_dir.mkdir(parents=True, exist_ok=True)
    method = None

    if module._install_gclient_from_submodule(bin_dir):
        method = "workspace"
    elif module._install_gclient_from_github(bin_dir, target, target_version):
        method = "github"
    elif module._install_gclient_from_cargo_binstall(bin_dir, target_version):
        method = "cargo-binstall"
    elif module._install_gclient_from_cargo_install(bin_dir, target_version):
        method = "cargo-install"
    elif module._install_gclient_from_cargo_git(bin_dir):
        method = "cargo-git"
    else:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    gclient_path.chmod(0o755)

    resolved_version = probe_gclient_version(module, gclient_path) or target_version or "unknown"
    module._write_gclient_version_stamp(bin_dir, resolved_version)

    module._ensure_gobby_bin_on_path(bin_dir)

    is_upgrade = installed_version is not None and installed_version != resolved_version
    return {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": method,
    }
