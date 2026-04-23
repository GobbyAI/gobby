"""gsqz installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def get_latest_gsqz_version(module: Any) -> str | None:
    """Query crates.io for latest gsqz version."""
    try:
        req = module.Request(module._GSQZ_CRATES_API, headers={"User-Agent": "gobby-installer/1.0"})
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("gsqz: could not check latest version: %s", e)
        return None


def get_installed_gsqz_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gsqz version from stamp file."""
    stamp = bin_dir / module._GSQZ_VERSION_STAMP
    binary = bin_dir / module._GSQZ_BIN_NAME
    if stamp.exists():
        content = stamp.read_text().strip()
        return content if content else None
    if binary.exists():
        return "unknown"
    return None


def write_gsqz_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gsqz version stamp atomically."""
    stamp = bin_dir / module._GSQZ_VERSION_STAMP
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gsqz-version-",
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


def install_gsqz_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract gsqz from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GSQZ_BIN_NAME,
            artifact_name="gsqz",
            target=target,
            version=version,
            tag_prefix=module._GSQZ_RELEASE_TAG_PREFIX,
            label="gsqz",
        )
    )


def install_gsqz_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install gsqz via cargo-binstall."""
    if not module.shutil.which("cargo-binstall"):
        return False
    try:
        crate = f"gobby-squeeze@{version}" if version else "gobby-squeeze"
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
        module.logger.warning("gsqz: cargo-binstall failed: %s", e)
        return False


def install_gsqz_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install gsqz from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    try:
        cmd = ["cargo", "install", "gobby-squeeze", "--root", str(bin_dir.parent)]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling gsqz from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gsqz: cargo install failed: %s", e)
        return False


def ensure_gobby_bin_on_path(module: Any) -> dict[str, Any]:
    """Add ~/.gobby/bin to PATH if shell rc does not already contain it."""
    gobby_bin = str(module.Path.home() / ".gobby" / "bin")
    result: dict[str, Any] = {"added": False}

    if gobby_bin in module.os.environ.get("PATH", "").split(module.os.pathsep):
        return result

    if module.sys.platform == "win32":
        module.click.echo(
            f"  Add {gobby_bin} to your PATH manually (System > Environment Variables)"
        )
        return result

    shell = module.os.environ.get("SHELL", "")
    shell_name = module.Path(shell).name if shell else ""

    rc_configs: dict[str, tuple[Path, str]] = {
        "zsh": (
            module.Path.home() / ".zshrc",
            'export PATH="$HOME/.gobby/bin:$PATH"  # gobby\n',
        ),
        "bash": (
            module.Path.home() / ".bashrc",
            'export PATH="$HOME/.gobby/bin:$PATH"  # gobby\n',
        ),
        "fish": (
            module.Path.home() / ".config" / "fish" / "config.fish",
            "fish_add_path ~/.gobby/bin  # gobby\n",
        ),
    }

    if shell_name not in rc_configs:
        module.logger.debug("gsqz: unknown shell %s, skipping PATH setup", shell_name)
        return result

    rc_file, export_line = rc_configs[shell_name]
    if rc_file.exists():
        content = rc_file.read_text()
        if "# gobby" in content and ".gobby/bin" in content:
            return result

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    with open(rc_file, "a") as f:
        f.write(f"\n{export_line}")

    result["added"] = True
    result["shell"] = shell_name
    result["rc_file"] = str(rc_file)
    return result


def install_gsqz(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade gsqz with GitHub/cargo fallback chain."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    gsqz_path = bin_dir / module._GSQZ_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._PLATFORM_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("gsqz: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_gsqz_version(bin_dir)
    latest_version = module._get_latest_gsqz_version()

    if gsqz_path.exists() and not force:
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

    if module._install_gsqz_from_github(bin_dir, target, target_version):
        method = "github"
    elif module._install_gsqz_from_cargo_binstall(bin_dir, target_version):
        method = "cargo-binstall"
    elif module._install_gsqz_from_cargo_install(bin_dir, target_version):
        method = "cargo-install"
    else:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    gsqz_path.chmod(0o755)
    resolved_version = target_version or "unknown"
    module._write_gsqz_version_stamp(bin_dir, resolved_version)

    path_result = module._ensure_gobby_bin_on_path()
    if path_result.get("added"):
        module.click.echo(
            f"  Added ~/.gobby/bin to PATH in {path_result['rc_file']} (restart shell or source it)"
        )

    is_upgrade = installed_version is not None and installed_version != resolved_version
    return {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": method,
    }
