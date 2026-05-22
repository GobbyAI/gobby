"""gloc installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.cli.install_setup_versions import managed_version_satisfies_pin


def get_latest_gloc_version(module: Any) -> str | None:
    """Query crates.io for latest gloc version."""
    try:
        req = module.Request(module._GLOC_CRATES_API, headers={"User-Agent": "gobby-installer/1.0"})
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("gloc: could not check latest version: %s", e)
        return None


def get_installed_gloc_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gloc version from stamp file."""
    stamp = bin_dir / module._GLOC_VERSION_STAMP
    binary = bin_dir / module._GLOC_BIN_NAME
    if stamp.exists():
        content = stamp.read_text().strip()
        return content if content else None
    if binary.exists():
        return "unknown"
    return None


def write_gloc_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gloc version stamp atomically."""
    stamp = bin_dir / module._GLOC_VERSION_STAMP
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".gloc-version-",
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


def install_gloc_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract gloc from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GLOC_BIN_NAME,
            artifact_name="gloc",
            target=target,
            version=version,
            tag_prefix=module._GLOC_RELEASE_TAG_PREFIX,
            label="gloc",
        )
    )


def install_gloc_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install gloc via cargo-binstall."""
    if not module.shutil.which("cargo-binstall"):
        return False
    try:
        crate = f"gobby-local@{version}" if version else "gobby-local"
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
        module.logger.warning("gloc: cargo-binstall failed: %s", e)
        return False


def install_gloc_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install gloc from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    try:
        cmd = ["cargo", "install", "gobby-local", "--root", str(bin_dir.parent)]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling gloc from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gloc: cargo install failed: %s", e)
        return False


def probe_gloc_version(module: Any, gloc_path: Path) -> str | None:
    """Probe gloc binary for a version string."""
    try:
        result = module.subprocess.run(
            [str(gloc_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        module.logger.warning("gloc: failed running --version probe: %s", e)
        return None

    if result.returncode != 0:
        module.logger.warning("gloc: --version probe failed: %s", result.stderr.strip())
        return None

    output = (result.stdout or result.stderr).strip()
    return output.split()[-1] if output else None


def install_gloc(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade gloc with public release fallback chain."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    gloc_path = bin_dir / module._GLOC_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._GLOC_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("gloc: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_gloc_version(bin_dir)
    target_version = module._get_latest_gloc_version()

    if gloc_path.exists() and not force:
        if managed_version_satisfies_pin("gloc", installed_version):
            return {"installed": False, "skipped": True, "version": installed_version}

    bin_dir.mkdir(parents=True, exist_ok=True)
    method = None

    if module._install_gloc_from_github(bin_dir, target, target_version):
        method = "github"
    elif module._install_gloc_from_cargo_binstall(bin_dir, target_version):
        method = "cargo-binstall"
    elif module._install_gloc_from_cargo_install(bin_dir, target_version):
        method = "cargo-install"

    if method is None:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    gloc_path.chmod(0o755)

    resolved_version = module._probe_gloc_version(gloc_path) or target_version or "unknown"
    module._write_gloc_version_stamp(bin_dir, resolved_version)

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
