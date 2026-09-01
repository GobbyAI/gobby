"""gwiki installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.cli.install_setup_versions import managed_version_satisfies_pin
from gobby.install.bin_freshness_models import compare_versions
from gobby.install.bin_set_coherence import promote_workspace_binary_set
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.install.version_probe import probe_native_bin_version


def get_latest_gwiki_version(module: Any) -> str | None:
    """Query crates.io for latest gwiki version."""
    try:
        req = module.Request(
            module._GWIKI_CRATES_API, headers={"User-Agent": "gobby-installer/1.0"}
        )
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("gwiki: could not check latest version: %s", e)
        return None


def get_installed_gwiki_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed gwiki version, preferring the binary over the stamp."""
    gwiki_path = bin_dir / module._GWIKI_BIN_NAME
    if gwiki_path.exists():
        probed_version = probe_gwiki_version(module, gwiki_path)
        if probed_version:
            return probed_version

    stamp = bin_dir / module._GWIKI_VERSION_STAMP
    try:
        return stamp.read_text().strip() if stamp.exists() else None
    except OSError:
        return None


def probe_gwiki_version(module: Any, gwiki_path: Path) -> str | None:
    """Probe gwiki binary for a version string."""
    return probe_native_bin_version(
        gwiki_path,
        runner=module.subprocess.run,
        logger=module.logger,
        label="gwiki",
    )


def write_gwiki_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write gwiki version stamp file."""
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        stamp = bin_dir / module._GWIKI_VERSION_STAMP
        stamp.write_text(f"{version}\n", encoding="utf-8")
    except OSError as e:
        module.logger.warning("gwiki: failed writing version stamp: %s", e)


def install_gwiki_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract gwiki from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GWIKI_BIN_NAME,
            artifact_name="gwiki",
            target=target,
            version=version,
            tag_prefix=module._GWIKI_RELEASE_TAG_PREFIX,
            label="gwiki",
        )
    )


def install_gwiki_from_submodule(module: Any, bin_dir: Path) -> bool:
    """Build gwiki from the local Rust workspace when available."""
    if not module.shutil.which("cargo"):
        return False

    search = Path(__file__).resolve().parent
    for _ in range(10):
        manifest = search / "Cargo.toml"
        crate_manifest = search / "crates" / "gwiki" / "Cargo.toml"
        if manifest.exists() and crate_manifest.exists():
            break
        search = search.parent
    else:
        module.logger.debug(
            "gwiki workspace not found after searching %d parents from %s",
            10,
            Path(__file__).resolve().parent,
        )
        return False

    try:
        module.click.echo("  Building gwiki from local workspace (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "gobby-wiki",
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
        src_bin = release_dir / module._GWIKI_BIN_NAME
        if not src_bin.exists():
            return False

        promote_workspace_binary_set({"gwiki": src_bin}, bin_dir=bin_dir)
        return True
    except (FileNotFoundError, module.subprocess.TimeoutExpired, OSError) as e:
        module.logger.warning("gwiki: local workspace build failed: %s", e)
        return False


def install_gwiki_from_cargo_git(module: Any, bin_dir: Path, version: str | None = None) -> bool:
    """Install gwiki from source via ``cargo install --git``, pinned to a tag.

    Without a tag, ``cargo install --git`` builds the repository HEAD — the one
    install path that ignores the managed version pin. Pin it to the
    ``gwiki-v<version>`` release tag (defaulting to
    ``MANAGED_BIN_VERSION_PINS['gwiki']``) so every fallback resolves the same
    version as the GitHub and cargo-binstall/cargo-install paths.
    """
    if not module.shutil.which("cargo"):
        return False
    target_version = version or MANAGED_BIN_VERSION_PINS["gwiki"]
    tag = f"{module._GWIKI_RELEASE_TAG_PREFIX}{target_version}"
    try:
        module.click.echo("  Compiling gwiki from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            [
                "cargo",
                "install",
                "--git",
                "https://github.com/GobbyAI/gobby",
                "--tag",
                tag,
                "-p",
                "gobby-wiki",
                "--root",
                str(bin_dir.parent),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gwiki: cargo install --git failed: %s", e)
        return False


def install_gwiki_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install gwiki via cargo-binstall."""
    if not module.shutil.which("cargo-binstall"):
        return False
    try:
        crate = f"gobby-wiki@{version}" if version else "gobby-wiki"
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
        module.logger.warning("gwiki: cargo-binstall failed: %s", e)
        return False


def install_gwiki_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install gwiki from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    try:
        cmd = ["cargo", "install", "gobby-wiki", "--root", str(bin_dir.parent)]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling gwiki from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("gwiki: cargo install failed: %s", e)
        return False


def install_gwiki(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade gwiki with dev-first fallback chain."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    gwiki_path = bin_dir / module._GWIKI_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._GWIKI_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("gwiki: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_gwiki_version(bin_dir)
    pinned_version = MANAGED_BIN_VERSION_PINS["gwiki"]
    if gwiki_path.exists() and not force:
        if installed_version and managed_version_satisfies_pin("gwiki", installed_version):
            module._write_gwiki_version_stamp(bin_dir, installed_version)
            return {"installed": False, "skipped": True, "version": installed_version}

    target_version = pinned_version
    if compare_versions(installed_version, pinned_version) == 1:
        target_version = installed_version
    bin_dir.mkdir(parents=True, exist_ok=True)
    method = None

    if module._install_gwiki_from_submodule(bin_dir):
        method = "workspace"
    elif module._install_gwiki_from_github(bin_dir, target, target_version):
        method = "github"
    elif module._install_gwiki_from_cargo_binstall(bin_dir, target_version):
        method = "cargo-binstall"
    elif module._install_gwiki_from_cargo_install(bin_dir, target_version):
        method = "cargo-install"
    elif module._install_gwiki_from_cargo_git(bin_dir, target_version):
        method = "cargo-git"
    else:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    if not gwiki_path.is_file():
        module.logger.warning(
            "gwiki: %s installer reported success but %s was not created",
            method,
            gwiki_path,
        )
        return {
            "installed": False,
            "skipped": False,
            "reason": f"{method} installer did not create {gwiki_path}",
        }

    gwiki_path.chmod(0o755)

    resolved_version = probe_gwiki_version(module, gwiki_path) or target_version or "unknown"
    module._write_gwiki_version_stamp(bin_dir, resolved_version)

    module._ensure_gobby_bin_on_path(bin_dir)

    is_upgrade = installed_version is not None and installed_version != resolved_version
    return {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": method,
    }
