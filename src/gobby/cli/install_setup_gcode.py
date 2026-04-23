"""gcode installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def get_latest_gcode_version(module: Any) -> str | None:
    """Query crates.io for latest gcode version."""
    try:
        req = module.Request(
            module._GCODE_CRATES_API,
            headers={"User-Agent": "gobby-installer/1.0"},
        )
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("gcode: could not check latest version: %s", e)
        return None


def get_installed_gcode_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gcode version from stamp file."""
    stamp = bin_dir / module._GCODE_VERSION_STAMP
    try:
        return stamp.read_text().strip() if stamp.exists() else None
    except OSError:
        return None


def write_gcode_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gcode version stamp atomically."""
    stamp = str(bin_dir / module._GCODE_VERSION_STAMP)
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gcode-version-",
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


def install_gcode_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract gcode from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GCODE_BIN_NAME,
            artifact_name="gcode",
            target=target,
            version=version,
            tag_prefix=module._GCODE_RELEASE_TAG_PREFIX,
            label="gcode",
        )
    )


def install_gcode_from_submodule(module: Any, bin_dir: Path) -> bool:
    """Build gcode from deps/gobby-cli submodule when available."""
    if not module.shutil.which("cargo"):
        return False

    search = Path(__file__).resolve().parent
    for _ in range(10):
        manifest = search / "deps" / "gobby-cli" / "Cargo.toml"
        if manifest.exists():
            break
        search = search.parent
    else:
        module.logger.debug(
            "gcode submodule not found after searching %d parents from %s",
            10,
            Path(__file__).resolve().parent,
        )
        return False

    try:
        module.click.echo("  Building gcode from submodule (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "gcode",
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
        src_bin = release_dir / module._GCODE_BIN_NAME
        if not src_bin.exists():
            return False

        bin_dir.mkdir(parents=True, exist_ok=True)
        dest = bin_dir / module._GCODE_BIN_NAME
        module.copy2(str(src_bin), str(dest))
        dest.chmod(0o755)
        return True
    except (FileNotFoundError, module.subprocess.TimeoutExpired, OSError) as e:
        module.logger.warning("gcode: submodule build failed: %s", e)
        return False


def install_gcode_from_cargo_git(module: Any, bin_dir: Path) -> bool:
    """Install gcode from source via cargo install --git."""
    if not module.shutil.which("cargo"):
        return False
    try:
        module.click.echo("  Compiling gcode from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            [
                "cargo",
                "install",
                "--git",
                "https://github.com/GobbyAI/gobby-cli",
                "-p",
                "gcode",
                "--root",
                str(bin_dir.parent),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gcode: cargo install --git failed: %s", e)
        return False


def install_gcode_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install gcode via cargo-binstall."""
    if not module.shutil.which("cargo-binstall"):
        return False
    try:
        crate = f"gobby-code@{version}" if version else "gobby-code"
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
        module.logger.warning("gcode: cargo-binstall failed: %s", e)
        return False


def install_gcode_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install gcode from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    try:
        cmd = ["cargo", "install", "gobby-code", "--root", str(bin_dir.parent)]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling gcode from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gcode: cargo install failed: %s", e)
        return False


def install_gcode(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade gcode with dev-first fallback chain."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    gcode_path = bin_dir / module._GCODE_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._GCODE_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("gcode: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_gcode_version(bin_dir)
    latest_version = module._get_latest_gcode_version()

    if gcode_path.exists() and not force:
        if installed_version and latest_version and installed_version == latest_version:
            return {"installed": False, "skipped": True, "version": installed_version}
        if installed_version and installed_version != "unknown" and latest_version is None:
            return {
                "installed": False,
                "skipped": True,
                "version": installed_version,
                "reason": "version check failed, keeping current",
            }

    target_version = latest_version
    bin_dir.mkdir(parents=True, exist_ok=True)
    method = None

    if module._install_gcode_from_submodule(bin_dir):
        method = "submodule"
    elif module._install_gcode_from_github(bin_dir, target, target_version):
        method = "github"
    elif module._install_gcode_from_cargo_binstall(bin_dir, target_version):
        method = "cargo-binstall"
    elif module._install_gcode_from_cargo_install(bin_dir, target_version):
        method = "cargo-install"
    elif module._install_gcode_from_cargo_git(bin_dir):
        method = "cargo-git"
    else:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    gcode_path.chmod(0o755)

    resolved_version = target_version
    if not resolved_version:
        try:
            result = module.subprocess.run(
                [str(gcode_path), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                resolved_version = parts[-1] if parts else "unknown"
            else:
                resolved_version = "unknown"
        except Exception:
            resolved_version = "unknown"
    module._write_gcode_version_stamp(bin_dir, resolved_version)

    module._ensure_gobby_bin_on_path()

    is_upgrade = installed_version is not None and installed_version != resolved_version
    return {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": method,
    }
