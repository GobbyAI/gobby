"""ghook installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.cli.install_setup_versions import managed_version_satisfies_pin

_NATIVE_GHOOK_BINARY_MAGICS = (
    b"\x7fELF",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
)


def get_latest_ghook_version(module: Any) -> str | None:
    """Query crates.io for latest ghook version."""
    try:
        req = module.Request(
            module._GHOOK_CRATES_API, headers={"User-Agent": "gobby-installer/1.0"}
        )
        with module._urlopen_https(req, timeout=10) as resp:
            data = module.json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (module.URLError, module.json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("ghook: could not check latest version: %s", e)
        return None


def get_installed_ghook_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed ghook version from stamp file."""
    stamp = bin_dir / module._GHOOK_VERSION_STAMP
    binary = bin_dir / module._GHOOK_BIN_NAME
    if stamp.exists():
        content = stamp.read_text().strip()
        return content if content else None
    if binary.exists():
        return "unknown"
    return None


def write_ghook_version_stamp(module: Any, bin_dir: Path, version: str) -> None:
    """Write ghook version stamp atomically."""
    stamp = bin_dir / module._GHOOK_VERSION_STAMP
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".ghook-version-",
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


def is_native_ghook_binary(module: Any, ghook_path: Path) -> bool:
    """Return whether the ghook path looks like a native executable."""
    try:
        header = ghook_path.read_bytes()[:4]
    except OSError as e:
        module.logger.debug("ghook: could not inspect existing binary %s: %s", ghook_path, e)
        return False
    return any(header.startswith(magic) for magic in _NATIVE_GHOOK_BINARY_MAGICS)


def ghook_installed_at_utc(module: Any) -> str:
    """Return current UTC timestamp with second precision and trailing Z."""
    return str(
        module.datetime.now(module.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def ghook_install_source_url(
    module: Any,
    method: str,
    *,
    target: str,
    version: str | None,
) -> str | None:
    """Return public install source URL for ghook provenance."""
    if method == "github":
        if not version:
            return None
        return str(
            module._build_release_download_url(
                "ghook",
                target,
                version=version,
                tag_prefix=module._GHOOK_RELEASE_TAG_PREFIX,
            )
        )
    if method == "cargo-binstall":
        if version and version != "unknown":
            return f"https://crates.io/crates/gobby-hooks/{version}"
        return "https://crates.io/crates/gobby-hooks"
    return None


def write_ghook_install_sidecar(
    module: Any,
    bin_dir: Path,
    *,
    install_method: str,
    install_source_url: str | None,
    installed_version: str,
    installed_at: str,
) -> None:
    """Best-effort provenance sidecar for ghook installs."""
    sidecar = bin_dir / module._GHOOK_INSTALL_SIDECAR
    fd, tmp_path = module.tempfile.mkstemp(
        dir=str(bin_dir),
        prefix=".ghook-install-",
        suffix=".tmp",
    )
    try:
        with module.os.fdopen(fd, "w", encoding="utf-8") as f:
            module.json.dump(
                {
                    "install_method": install_method,
                    "install_source_url": install_source_url,
                    "installed_version": installed_version,
                    "installed_at": installed_at,
                },
                f,
            )
            f.write("\n")
            f.flush()
            module.os.fsync(f.fileno())
        module.os.replace(tmp_path, sidecar)
        module.os.chmod(sidecar, 0o644)
    except Exception as e:
        if module.os.path.exists(tmp_path):
            module.os.unlink(tmp_path)
        module.logger.warning("ghook: failed writing install sidecar %s: %s", sidecar, e)


def install_ghook_from_github(
    module: Any,
    bin_dir: Path,
    target: str,
    version: str | None = None,
) -> bool:
    """Download and extract ghook from GitHub Releases."""
    return bool(
        module._download_release_binary(
            bin_dir,
            binary_name=module._GHOOK_BIN_NAME,
            artifact_name="ghook",
            target=target,
            version=version,
            tag_prefix=module._GHOOK_RELEASE_TAG_PREFIX,
            label="ghook",
        )
    )


def install_ghook_from_cargo_binstall(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Install ghook via cargo-binstall."""
    if not module.shutil.which("cargo-binstall"):
        return False
    try:
        crate = f"gobby-hooks@{version}" if version else "gobby-hooks"
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
        module.logger.warning("ghook: cargo-binstall failed: %s", e)
        return False


def install_ghook_from_cargo_install(
    module: Any,
    bin_dir: Path,
    version: str | None = None,
) -> bool:
    """Compile and install ghook from source via cargo install."""
    if not module.shutil.which("cargo"):
        return False
    try:
        cmd = ["cargo", "install", "gobby-hooks", "--root", str(bin_dir.parent)]
        if version:
            cmd.extend(["--version", version])
        module.click.echo("  Compiling ghook from source (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(result.returncode == 0)
    except (FileNotFoundError, module.subprocess.TimeoutExpired) as e:
        module.logger.warning("ghook: cargo install failed: %s", e)
        return False


def get_ghook_version_override(module: Any) -> str | None:
    """Return explicit ghook version override from environment."""
    value = str(module.os.environ.get(module._GHOOK_INSTALL_VERSION_ENV, "")).strip()
    return value or None


def get_ghook_method_override(module: Any) -> str | None:
    """Return explicit ghook install-method override from environment."""
    value = str(module.os.environ.get(module._GHOOK_INSTALL_METHOD_ENV, "")).strip().lower()
    if not value or value == "auto":
        return None
    if value not in module._GHOOK_ALLOWED_METHODS:
        module.logger.warning(
            "ghook: ignoring unsupported %s=%s",
            module._GHOOK_INSTALL_METHOD_ENV,
            value,
        )
        return None
    return str(value)


def probe_ghook_version(module: Any, ghook_path: Path) -> str | None:
    """Probe ghook binary for version and compatibility side effects."""
    try:
        result = module.subprocess.run(
            [str(ghook_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        module.logger.warning("ghook: failed running --version probe: %s", e)
        return None

    if result.returncode != 0:
        module.logger.warning("ghook: --version probe failed: %s", result.stderr.strip())
        return None

    output = (result.stdout or result.stderr).strip()
    return output.split()[-1] if output else None


def install_ghook(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade ghook with public release fallback chain."""
    bin_dir = module.Path.home() / ".gobby" / "bin"
    ghook_path = bin_dir / module._GHOOK_BIN_NAME

    os_name = module.sys.platform
    machine = module.platform.machine().lower()
    target = module._GHOOK_TARGETS.get((os_name, machine))
    if target is None:
        module.logger.warning("ghook: unsupported platform %s/%s", os_name, machine)
        return {
            "installed": False,
            "skipped": True,
            "reason": f"unsupported platform {os_name}/{machine}",
        }

    installed_version = module._get_installed_ghook_version(bin_dir)
    requested_version = module._get_ghook_version_override()
    target_version = requested_version or module._get_latest_ghook_version()
    method_override = module._get_ghook_method_override()

    if ghook_path.exists() and not force:
        if not module._is_native_ghook_binary(ghook_path):
            module.logger.warning(
                "ghook: existing %s is not a native executable; reinstalling",
                ghook_path,
            )
        elif managed_version_satisfies_pin("ghook", installed_version):
            return {"installed": False, "skipped": True, "version": installed_version}

    bin_dir.mkdir(parents=True, exist_ok=True)
    method = None

    if method_override == "github":
        if module._install_ghook_from_github(bin_dir, target, target_version):
            method = "github"
    elif method_override == "cargo-binstall":
        if module._install_ghook_from_cargo_binstall(bin_dir, target_version):
            method = "cargo-binstall"
    elif method_override == "cargo-install":
        if module._install_ghook_from_cargo_install(bin_dir, target_version):
            method = "cargo-install"
    else:
        if module._install_ghook_from_github(bin_dir, target, target_version):
            method = "github"
        elif module._install_ghook_from_cargo_binstall(bin_dir, target_version):
            method = "cargo-binstall"
        elif module._install_ghook_from_cargo_install(bin_dir, target_version):
            method = "cargo-install"

    if method is None:
        return {"installed": False, "skipped": False, "reason": "all installation methods failed"}

    ghook_path.chmod(0o755)

    resolved_version = module._probe_ghook_version(ghook_path) or target_version or "unknown"
    module._write_ghook_version_stamp(bin_dir, resolved_version)
    sidecar_version = resolved_version if resolved_version != "unknown" else target_version
    module._write_ghook_install_sidecar(
        bin_dir,
        install_method=module._GHOOK_PUBLIC_INSTALL_METHODS[method],
        install_source_url=module._ghook_install_source_url(
            method,
            target=target,
            version=sidecar_version,
        ),
        installed_version=resolved_version,
        installed_at=module._ghook_installed_at_utc(),
    )

    path_result = module._ensure_gobby_bin_on_path()
    if path_result.get("added"):
        module.click.echo(
            f"  Added ~/.gobby/bin to PATH in {path_result['rc_file']} (restart shell or source it)"
        )

    is_upgrade = installed_version is not None and installed_version != resolved_version
    result = {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": method,
    }
    compatibility_stamp = bin_dir / module._GHOOK_COMPATIBILITY_STAMP
    if compatibility_stamp.exists():
        result["compatibility"] = str(compatibility_stamp)
    return result
