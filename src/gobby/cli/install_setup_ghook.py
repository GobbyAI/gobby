"""ghook installer implementations used by install_setup compatibility wrappers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request

from gobby.agents.cargo_target import cargo_release_dir
from gobby.cli.install_setup_versions import managed_version_satisfies_pin
from gobby.install.bin_freshness_models import compare_versions
from gobby.install.bin_set_coherence import (
    promote_workspace_binary_set,
    workspace_build_only,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.install.version_probe import probe_native_bin_version

from . import install_release

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
        req = Request(module._GHOOK_CRATES_API, headers={"User-Agent": "gobby-installer/1.0"})
        with install_release._urlopen_https(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return str(data["crate"]["max_version"])
    except (URLError, json.JSONDecodeError, KeyError, OSError) as e:
        module.logger.debug("ghook: could not check latest version: %s", e)
        return None


def get_installed_ghook_version(module: Any, bin_dir: Path) -> str | None:
    """Read installed ghook version, preferring the binary over the stamp."""
    binary = bin_dir / module._GHOOK_BIN_NAME
    if binary.exists():
        probed_version = probe_ghook_version(module, binary)
        if probed_version:
            return probed_version

    stamp = bin_dir / module._GHOOK_VERSION_STAMP
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
        with ghook_path.open("rb") as f:
            header = f.read(4)
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
            json.dump(
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


def install_ghook_from_workspace(module: Any, bin_dir: Path) -> bool:
    """Build ghook from the local Rust workspace and promote it into the set."""
    if not module.shutil.which("cargo"):
        return False
    current = Path(__file__).resolve().parent
    manifest = next(
        (
            parent / "Cargo.toml"
            for parent in (current, *current.parents)
            if (parent / "Cargo.toml").is_file()
            and (parent / "crates" / "ghook" / "Cargo.toml").is_file()
        ),
        None,
    )
    if manifest is None:
        return False
    try:
        module.click.echo("  Building ghook from local workspace (this may take 30-60 seconds)...")
        result = module.subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "gobby-hooks",
                "--manifest-path",
                str(manifest),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            return False
        source = cargo_release_dir(manifest.parent) / module._GHOOK_BIN_NAME
        if not source.exists():
            return False
        promote_workspace_binary_set({"ghook": source}, bin_dir=bin_dir)
        return True
    except (FileNotFoundError, module.subprocess.TimeoutExpired, OSError) as e:
        module.logger.warning("ghook: local workspace build failed: %s", e)
        return False


def probe_ghook_version(module: Any, ghook_path: Path) -> str | None:
    """Probe ghook binary for a version string."""
    return probe_native_bin_version(
        ghook_path,
        runner=module.subprocess.run,
        logger=module.logger,
        label="ghook",
    )


def install_ghook(module: Any, force: bool = False) -> dict[str, Any]:
    """Install or upgrade ghook from the workspace build, its only source."""
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
    pinned_version = MANAGED_BIN_VERSION_PINS["ghook"]

    if ghook_path.exists() and not force:
        if not module._is_native_ghook_binary(ghook_path):
            module.logger.warning(
                "ghook: existing %s is not a native executable; reinstalling",
                ghook_path,
            )
        elif installed_version and managed_version_satisfies_pin("ghook", installed_version):
            module._write_ghook_version_stamp(bin_dir, installed_version)
            return {"installed": False, "skipped": True, "version": installed_version}

    target_version = pinned_version
    if compare_versions(installed_version, pinned_version) == 1:
        target_version = installed_version

    bin_dir.mkdir(parents=True, exist_ok=True)
    if not module._install_ghook_from_workspace(bin_dir):
        return {"installed": False, "skipped": False, "reason": workspace_build_only("ghook")}

    ghook_path.chmod(0o755)

    resolved_version = module._probe_ghook_version(ghook_path) or target_version or "unknown"
    module._write_ghook_version_stamp(bin_dir, resolved_version)
    module._write_ghook_install_sidecar(
        bin_dir,
        install_method="workspace",
        install_source_url=None,
        installed_version=resolved_version,
        installed_at=module._ghook_installed_at_utc(),
    )

    path_result = module._ensure_gobby_bin_on_path(bin_dir)
    if path_result.get("added"):
        module.click.echo(
            f"  Added ~/.gobby/bin to PATH in {path_result['rc_file']} (restart shell or source it)"
        )

    is_upgrade = installed_version is not None and installed_version != resolved_version
    result = {
        "installed": True,
        "upgraded": is_upgrade,
        "version": resolved_version,
        "method": "workspace",
    }
    runtime_stamp = bin_dir / module._GHOOK_RUNTIME_STAMP
    if runtime_stamp.exists():
        result["runtime_stamp"] = str(runtime_stamp)
    return result
