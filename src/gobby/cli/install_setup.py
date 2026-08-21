"""Helpers for install-time daemon bootstrap and bundled binary installs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess  # nosec B404 # fixed npm and managed helper invocations
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click

from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT
from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG
from gobby.install.bin_freshness_github import SourceUnavailableError
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_freshness_promotion import stage_and_promote_release_binary
from gobby.install.checksums import parse_sha256_digest
from gobby.install.distribution import (
    HomebrewDistributionError,
    is_homebrew_distribution,
    verify_homebrew_managed_bins,
)

from . import install_setup_gclient as _gclient_impl
from . import install_setup_gcode as _gcode_impl
from . import install_setup_ghook as _ghook_impl
from . import install_setup_gterm as _gterm_impl
from . import install_setup_gwiki as _gwiki_impl
from .install_setup_gdaemon import GdaemonInstallError, ensure_gdaemon
from .utils import get_install_dir

logger = logging.getLogger(__name__)
_HELPER_RELEASE_REPOSITORY = "GobbyAI/gobby"
# Helper modules resolve these names dynamically from this module to preserve
# existing patch targets in tests and callers.
_HELPER_EXPORTS = (os, platform, shutil, tempfile, UTC, datetime)


def _module() -> Any:
    return sys.modules[__name__]


def _urlopen_https(req: Request, *, timeout: int) -> Any:
    """Open a URL after validating the scheme is HTTPS.

    Wraps :func:`urllib.request.urlopen` with a scheme check to prevent
    ``file://`` or other unexpected schemes (bandit B310).
    """
    url = req.full_url
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS URLs are allowed, got: {parsed.scheme}://...")
    return urlopen(req, timeout=timeout)  # nosec B310 # scheme validated above


def _release_archive_extension(target: str) -> str:
    """Return the packaged release archive extension for a target triple."""
    return "zip" if "windows" in target else "tar.gz"


def _build_release_download_url(
    artifact_name: str,
    target: str,
    *,
    version: str | None,
    tag_prefix: str,
    resolved_tag: str | None = None,
) -> str:
    """Build the GitHub Releases download URL for a binary artifact."""
    archive_ext = _release_archive_extension(target)
    artifact_filename = f"{artifact_name}-{target}.{archive_ext}"
    tag_name = resolved_tag or (f"{tag_prefix}{version}" if version else None)
    if not tag_name:
        raise ValueError(f"No matching stable release found for tag prefix {tag_prefix!r}")
    return (
        f"https://github.com/{_HELPER_RELEASE_REPOSITORY}/releases/download/"
        f"{tag_name}/{artifact_filename}"
    )


def _parse_release_semver(version_text: str) -> tuple[int, ...] | None:
    """Parse a simple semver string into a sortable tuple when possible."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?", version_text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def _fetch_helper_releases(repository: str) -> list[dict[str, Any]]:
    """Fetch GitHub release metadata for a helper release repository."""
    req = Request(
        f"https://api.github.com/repos/{repository}/releases?per_page=100",
        headers={
            "User-Agent": "gobby-installer/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with _urlopen_https(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode("utf-8"))

    if not isinstance(releases, list):
        raise ValueError("GitHub Releases API returned an unexpected payload")
    return [release for release in releases if isinstance(release, dict)]


def _newest_stable_release_tag(
    releases: list[dict[str, Any]],
    *,
    tag_prefix: str,
) -> str | None:
    """Return the newest stable release tag in a release payload."""

    stable_matches: list[tuple[str, str]] = []
    semver_matches: list[tuple[tuple[int, ...], str, str]] = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag_name = release.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name.startswith(tag_prefix):
            continue
        published_at = release.get("published_at")
        published_sort = published_at if isinstance(published_at, str) else ""
        stable_matches.append((tag_name, published_sort))
        semver = _parse_release_semver(tag_name[len(tag_prefix) :])
        if semver is not None:
            semver_matches.append((semver, published_sort, tag_name))

    if semver_matches:
        return max(semver_matches, key=lambda item: (item[0], item[1]))[2]
    if stable_matches:
        return max(stable_matches, key=lambda item: item[1])[0]
    return None


def _resolve_latest_release_tag(*, tag_prefix: str) -> str:
    """Resolve the newest stable GitHub release tag for a tag prefix."""
    try:
        tag = _newest_stable_release_tag(
            _fetch_helper_releases(_HELPER_RELEASE_REPOSITORY),
            tag_prefix=tag_prefix,
        )
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"No matching stable release found for tag prefix {tag_prefix!r} "
            f"({_HELPER_RELEASE_REPOSITORY}: {exc})"
        ) from exc
    if tag is None:
        raise ValueError(f"No matching stable release found for tag prefix {tag_prefix!r}")
    return tag


def _extract_binary_from_release_archive(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    bin_dir: Path,
    label: str,
) -> bool:
    """Extract one binary from a release archive into ``bin_dir``."""
    lock = try_acquire_native_bin_lock(label, bin_dir=bin_dir)
    if lock is None:
        logger.warning("%s: native binary update is already in progress", label)
        return False
    try:
        with lock:
            stage_and_promote_release_binary(
                archive_bytes,
                archive_ext=archive_ext,
                binary_name=binary_name,
                bin_dir=bin_dir,
                asset_name=label,
            )
        return True
    except (OSError, SourceUnavailableError) as e:
        logger.warning("%s: failed extracting release archive: %s", label, e)
        return False


def _fetch_release_checksum(checksum_url: str, *, label: str) -> str | None:
    """Fetch and parse the published SHA-256 digest for a release asset.

    Returns the lowercase hex digest, or ``None`` when the checksum file
    cannot be retrieved or parsed; the caller treats ``None`` as a
    verification failure (fail-closed).
    """
    try:
        req = Request(checksum_url, headers={"User-Agent": "gobby-installer/1.0"})
        with _urlopen_https(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    except (URLError, OSError, ValueError) as e:
        logger.warning("%s: could not fetch checksum %s: %s", label, checksum_url, e)
        return None
    return parse_sha256_digest(text)


def _verify_release_artifact(
    archive_bytes: bytes,
    *,
    checksum_url: str,
    label: str,
) -> bool:
    """Verify downloaded artifact bytes against the published SHA-256.

    Fetches the per-asset ``.sha256`` file at ``checksum_url``, parses the
    expected digest, and compares it to the SHA-256 of ``archive_bytes``.
    Returns ``False`` — fail-closed — when the checksum cannot be fetched or
    parsed, or when the digests differ, so the caller falls through to the
    next install method instead of executing an unverified binary.
    """
    expected = _fetch_release_checksum(checksum_url, label=label)
    if expected is None:
        logger.warning(
            "%s: no published checksum at %s; refusing unverified download",
            label,
            checksum_url,
        )
        return False
    actual = hashlib.sha256(archive_bytes).hexdigest()
    if actual != expected:
        logger.warning(
            "%s: checksum mismatch for %s (expected %s, got %s)",
            label,
            checksum_url,
            expected,
            actual,
        )
        return False
    return True


def _download_release_binary(
    bin_dir: Path,
    *,
    binary_name: str,
    artifact_name: str,
    target: str,
    version: str | None,
    tag_prefix: str,
    label: str,
) -> bool:
    """Download and extract a native binary from GitHub Releases."""
    archive_ext = _release_archive_extension(target)
    try:
        resolved_tag = (
            _resolve_latest_release_tag(tag_prefix=tag_prefix) if version is None else None
        )
        url = _build_release_download_url(
            artifact_name,
            target,
            version=version,
            tag_prefix=tag_prefix,
            resolved_tag=resolved_tag,
        )
        logger.info("Downloading %s from %s", label, url)
        req = Request(url, headers={"User-Agent": "gobby-installer/1.0"})
        with _urlopen_https(req, timeout=30) as resp:
            archive_bytes = resp.read()
        if not _verify_release_artifact(
            archive_bytes,
            checksum_url=f"{url}.sha256",
            label=label,
        ):
            return False
        return _extract_binary_from_release_archive(
            archive_bytes,
            archive_ext=archive_ext,
            binary_name=binary_name,
            bin_dir=bin_dir,
            label=label,
        )
    except (URLError, OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("%s: GitHub download failed: %s", label, e)
        return False


def ensure_daemon_config(*, files_home: str | Path | None = None) -> dict[str, Any]:
    """Ensure bootstrap config exists at ~/.gobby/bootstrap.yaml.

    Creating a local bootstrap requires an existing absolute ``files_home``.
    Publication uses exclusive-lock durable replace, not copy2.

    Returns:
        Dict with 'created' (bool) and 'path' (str) keys
    """
    from gobby.config.bootstrap import BootstrapConfigError, validate_existing_files_home
    from gobby.config.bootstrap_io import read_bootstrap_yaml, write_bootstrap_yaml

    bootstrap_path = Path("~/.gobby/bootstrap.yaml").expanduser()

    if bootstrap_path.exists():
        return {"created": False, "path": str(bootstrap_path)}

    if files_home is None:
        raise BootstrapConfigError(
            "Creating a local bootstrap requires an existing absolute files_home. "
            "Run `gobby install --files-home <absolute-dir>`."
        )
    validated = validate_existing_files_home(files_home)

    shared_bootstrap = get_install_dir() / "shared" / "config" / "bootstrap.yaml"
    if shared_bootstrap.exists():
        data = read_bootstrap_yaml(shared_bootstrap)
        source = "shared"
    else:
        data = {
            "database_url": "postgresql://gobby:gobby_dev@localhost:60891/gobby",
            "postgres_pool": DEFAULT_POSTGRES_POOL_CONFIG.to_dict(),
            "daemon_port": 60887,
            "bind_host": "localhost",
            "websocket_port": DEFAULT_WEBSOCKET_PORT,
            "ui_port": 60889,
        }
        source = "generated"
    data["datastore_mode"] = data.get("datastore_mode") or "local"
    data["files_home"] = str(validated)
    write_bootstrap_yaml(bootstrap_path, data)
    return {"created": True, "path": str(bootstrap_path), "source": source}


def run_daemon_setup(project_path: Path, *, configure_ide_settings: bool) -> None:
    """Run install setup: DB init, bundled content sync, MCP servers, IDE config.

    Called after ensure_daemon_config(). Handles database initialization,
    bundled content sync, default MCP server installation, and IDE config.

    Args:
        project_path: The project directory path (used for context only).
        configure_ide_settings: Whether to mutate detected VS Code-family settings.
    """
    from .installers import install_default_mcp_servers

    try:
        gdaemon_result = ensure_gdaemon()
    except (GdaemonInstallError, OSError, ValueError) as exc:
        raise click.ClickException(f"Failed to provision gdaemon: {exc}") from exc
    action = "Installed" if gdaemon_result["installed"] else "Verified"
    click.echo(f"{action} gdaemon {gdaemon_result['version']} via {gdaemon_result['method']}")

    try:
        from gobby.storage.hub.runtime import runtime_hub_database
        from gobby.sync_registry import sync_bundled_content_to_db

        with runtime_hub_database() as db:
            click.echo("PostgreSQL hub initialized")
            sync_result = sync_bundled_content_to_db(db)
            if sync_result["total_synced"] > 0:
                click.echo(f"Synced {sync_result['total_synced']} bundled items to database")
            if sync_result["errors"]:
                for err in sync_result["errors"]:
                    click.echo(f"  Warning: {err}")
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        raise click.ClickException(
            f"Database initialization failed ({type(exc).__name__}): {exc}"
        ) from exc

    mcp_result = install_default_mcp_servers()
    if mcp_result["success"]:
        if mcp_result["servers_added"]:
            click.echo(f"Added MCP servers to proxy: {', '.join(mcp_result['servers_added'])}")
        if mcp_result["servers_skipped"]:
            click.echo(
                f"MCP servers already configured: {', '.join(mcp_result['servers_skipped'])}"
            )
    else:
        click.echo(f"Warning: Failed to configure MCP servers: {mcp_result['error']}")

    from gobby.agents.srt_runtime import SrtRuntimeError

    from .install_setup_srt import install_srt_runtime

    try:
        srt_result = install_srt_runtime()
    except SrtRuntimeError as exc:
        click.echo(
            "Warning: Failed to install managed Sandbox Runtime "
            f"({exc}). Set agent_sandbox.backend = provider-native to use the "
            "provider sandbox until SRT is available."
        )
    else:
        action = "Installed" if srt_result.installed else "Verified"
        click.echo(f"{action} managed Sandbox Runtime {srt_result.version}: {srt_result.path}")

    homebrew_mode = is_homebrew_distribution()
    from .install_setup_impeccable import (
        ImpeccableInstallError,
        install_impeccable_cli,
        reconcile_impeccable_installation,
    )

    try:
        impeccable_result = install_impeccable_cli()
    except ImpeccableInstallError as exc:
        raise click.ClickException(f"Failed to provision managed Impeccable CLI: {exc}") from exc
    action = "Installed" if impeccable_result.installed else "Verified"
    click.echo(
        f"{action} managed Impeccable CLI {impeccable_result.version}: {impeccable_result.path}"
    )
    reconcile_impeccable_installation(project_path)

    if homebrew_mode:
        try:
            statuses = verify_homebrew_managed_bins()
        except HomebrewDistributionError as exc:
            raise click.ClickException(str(exc)) from exc
        helper_versions = ", ".join(f"{status.name} {status.version}" for status in statuses)
        click.echo(f"Verified Homebrew helper binaries: {helper_versions}")
        click.echo("Skipping global npm installs in Homebrew distribution mode")
    else:
        _run_npm_install("Playwright CLI", "@playwright/cli@latest", project_path)
        _run_npm_install("ClawHub CLI", "clawhub", project_path)

    if homebrew_mode:
        click.echo("Using Homebrew-provided native helper binaries")
    else:
        _run_managed_native_binary_installs()

    try:
        from .installers.tmux_config import configure_tmux_clipboard

        tmux_result = configure_tmux_clipboard()
        if tmux_result.get("updated"):
            click.echo(f"Configured tmux clipboard integration: {tmux_result['config_path']}")
        elif tmux_result.get("error"):
            click.echo(f"Warning: Failed to configure tmux clipboard: {tmux_result['error']}")
    except (ImportError, OSError, PermissionError, ValueError) as e:
        click.echo(f"Warning: Failed to configure tmux clipboard: {e}")

    if configure_ide_settings:
        try:
            from .installers.ide_config import configure_vscode_family_terminal_integration

            ide_results = configure_vscode_family_terminal_integration()
            configured_ides = [
                ide_name
                for ide_name, result in ide_results.items()
                if result.get("added") or result.get("updated")
            ]
            if configured_ides:
                click.echo(
                    f"Configured VS Code-family terminal integration: {', '.join(configured_ides)}"
                )
            for ide_name, result in ide_results.items():
                if result.get("warning"):
                    click.echo(
                        f"Warning: Skipped {ide_name} terminal integration: {result['warning']}"
                    )
                elif result.get("error"):
                    click.echo(
                        f"Warning: Failed to configure {ide_name} terminal integration: "
                        f"{result['error']}"
                    )
        except (ImportError, OSError, PermissionError, ValueError) as e:
            click.echo(f"Warning: Failed to configure VS Code-family terminal integration: {e}")


def _run_npm_install(label: str, package: str, project_path: Path) -> None:
    npm_executable = shutil.which("npm")
    if npm_executable is None:
        click.echo(f"Warning: npm not found — skipping {label} install")
        return
    try:
        npm_result = subprocess.run(  # nosec B603 # executable resolved with shutil.which
            [npm_executable, "install", "-g", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if npm_result.returncode == 0:
            click.echo(f"Installed {label} ({package.removesuffix('@latest')})")
        else:
            click.echo(f"Warning: Failed to install {label}: {npm_result.stderr.strip()}")
    except OSError as e:
        click.echo(f"Warning: Failed to run npm for {label}: {e}")
    except subprocess.TimeoutExpired:
        click.echo(f"Warning: {label} install timed out")


MANAGED_NATIVE_BINARY_NAMES: tuple[str, ...] = ("gcode", "ghook", "gwiki", "gterm", "gclient")
_MANAGED_NATIVE_BINARY_DESCRIPTIONS: dict[str, str] = {
    "gcode": "code index CLI",
    "ghook": "hook manager",
    "gwiki": "wiki CLI",
    "gterm": "terminal host",
    "gclient": "workspace client",
}


def _echo_managed_binary_install(name: str, result: dict[str, Any], description: str) -> None:
    if result.get("installed"):
        verb = "Upgraded" if result.get("upgraded") else "Installed"
        click.echo(
            f"{verb} {name} {result.get('version', '')} "
            f"via {result.get('method', 'unknown')} ({description})"
        )
        return
    if result.get("skipped"):
        reason = result.get("reason", "")
        suffix = f" ({reason})" if reason else ""
        click.echo(f"{name} already installed and up to date{suffix}")
        return
    reason = result.get("reason", "unknown error")
    click.echo(f"Warning: Failed to install {name}: {reason}")


def _run_managed_native_binary_installs() -> None:
    installers = {
        "gcode": _install_gcode,
        "ghook": _install_ghook,
        "gwiki": _install_gwiki,
        "gterm": _install_gterm,
        "gclient": _install_gclient,
    }
    for name in MANAGED_NATIVE_BINARY_NAMES:
        description = _MANAGED_NATIVE_BINARY_DESCRIPTIONS[name]
        try:
            _echo_managed_binary_install(name, installers[name](), description)
        except Exception as exc:
            click.echo(f"Warning: Failed to install {name}: {exc}")


# Platform -> target triple mapping used across public binary installers.
_PLATFORM_TARGETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("win32", "amd64"): "x86_64-pc-windows-msvc",
    ("win32", "arm64"): "aarch64-pc-windows-msvc",
}


def _ensure_gobby_bin_on_path(bin_dir: Path) -> dict[str, Any]:
    """Add the resolved Gobby bin directory to PATH through the shell rc."""
    gobby_bin = str(bin_dir.resolve(strict=False))
    result: dict[str, Any] = {"added": False}

    tmp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    if bin_dir.resolve(strict=False).is_relative_to(tmp_root):
        # An ephemeral bin dir (isolated GOBBY_HOME) must never land in the
        # user's real shell rc.
        logger.debug("skipping PATH setup for ephemeral bin dir %s", gobby_bin)
        return result

    if gobby_bin in os.environ.get("PATH", "").split(os.pathsep):
        return result

    if sys.platform == "win32":
        click.echo(f"  Add {gobby_bin} to your PATH manually (System > Environment Variables)")
        return result

    shell = os.environ.get("SHELL", "")
    shell_name = Path(shell).name if shell else ""

    rc_configs: dict[str, tuple[Path, str]] = {
        "zsh": (
            Path.home() / ".zshrc",
            f'export PATH={shlex.quote(gobby_bin)}:"$PATH"  # gobby\n',
        ),
        "bash": (
            Path.home() / ".bashrc",
            f'export PATH={shlex.quote(gobby_bin)}:"$PATH"  # gobby\n',
        ),
        "fish": (
            Path.home() / ".config" / "fish" / "config.fish",
            f"fish_add_path {shlex.quote(gobby_bin)}  # gobby\n",
        ),
    }

    if shell_name not in rc_configs:
        logger.debug("unknown shell %s, skipping PATH setup", shell_name)
        return result

    rc_file, export_line = rc_configs[shell_name]
    if rc_file.exists():
        try:
            content = rc_file.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("could not read shell rc file %s: %s", rc_file, exc)
            return result
        if "# gobby" in content and gobby_bin in content:
            return result

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    with open(rc_file, "a") as f:
        f.write(f"\n{export_line}")

    result["added"] = True
    result["shell"] = shell_name
    result["rc_file"] = str(rc_file)
    return result


_GCODE_RELEASE_TAG_PREFIX = "gcode-v"
_GCODE_VERSION_STAMP = ".gcode-version"
_GCODE_BIN_NAME = "gcode.exe" if sys.platform == "win32" else "gcode"
_GCODE_TARGETS = _PLATFORM_TARGETS
_GCODE_CRATES_API = "https://crates.io/api/v1/crates/gobby-code"


def _get_latest_gcode_version() -> str | None:
    return _gcode_impl.get_latest_gcode_version(_module())


def _get_installed_gcode_version(bin_dir: Path) -> str | None:
    return _gcode_impl.get_installed_gcode_version(_module(), bin_dir)


def _write_gcode_version_stamp(bin_dir: Path, version: str) -> None:
    _gcode_impl.write_gcode_version_stamp(_module(), bin_dir, version)


def _install_gcode_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _gcode_impl.install_gcode_from_github(_module(), bin_dir, target, version)


def _install_gcode_from_submodule(bin_dir: Path) -> bool:
    return _gcode_impl.install_gcode_from_submodule(_module(), bin_dir)


def _install_gcode_from_cargo_git(bin_dir: Path) -> bool:
    return _gcode_impl.install_gcode_from_cargo_git(_module(), bin_dir)


def _install_gcode_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _gcode_impl.install_gcode_from_cargo_binstall(_module(), bin_dir, version)


def _install_gcode_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _gcode_impl.install_gcode_from_cargo_install(_module(), bin_dir, version)


def _install_gcode(force: bool = False) -> dict[str, Any]:
    return _gcode_impl.install_gcode(_module(), force)


_GHOOK_RELEASE_TAG_PREFIX = "ghook-v"
_GHOOK_CRATES_API = "https://crates.io/api/v1/crates/gobby-hooks"
_GHOOK_VERSION_STAMP = ".ghook-version"
_GHOOK_INSTALL_SIDECAR = ".ghook-install.json"
_GHOOK_RUNTIME_STAMP = ".ghook-runtime.json"
_GHOOK_BIN_NAME = "ghook.exe" if sys.platform == "win32" else "ghook"
_GHOOK_TARGETS = _PLATFORM_TARGETS
_GHOOK_INSTALL_VERSION_ENV = "GOBBY_INSTALL_GHOOK_VERSION"
_GHOOK_INSTALL_METHOD_ENV = "GOBBY_INSTALL_GHOOK_METHOD"
_GHOOK_ALLOWED_METHODS = {"auto", "github", "cargo-binstall", "cargo-install"}
_GHOOK_PUBLIC_INSTALL_METHODS = {
    "github": "github-release",
    "cargo-binstall": "crates-binstall",
    "cargo-install": "cargo-install",
}


def _get_latest_ghook_version() -> str | None:
    return _ghook_impl.get_latest_ghook_version(_module())


def _get_installed_ghook_version(bin_dir: Path) -> str | None:
    return _ghook_impl.get_installed_ghook_version(_module(), bin_dir)


def _write_ghook_version_stamp(bin_dir: Path, version: str) -> None:
    _ghook_impl.write_ghook_version_stamp(_module(), bin_dir, version)


def _is_native_ghook_binary(ghook_path: Path) -> bool:
    return _ghook_impl.is_native_ghook_binary(_module(), ghook_path)


def _ghook_installed_at_utc() -> str:
    return _ghook_impl.ghook_installed_at_utc(_module())


def _ghook_install_source_url(method: str, *, target: str, version: str | None) -> str | None:
    return _ghook_impl.ghook_install_source_url(_module(), method, target=target, version=version)


def _write_ghook_install_sidecar(
    bin_dir: Path,
    *,
    install_method: str,
    install_source_url: str | None,
    installed_version: str,
    installed_at: str,
) -> None:
    _ghook_impl.write_ghook_install_sidecar(
        _module(),
        bin_dir,
        install_method=install_method,
        install_source_url=install_source_url,
        installed_version=installed_version,
        installed_at=installed_at,
    )


def _install_ghook_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _ghook_impl.install_ghook_from_github(_module(), bin_dir, target, version)


def _install_ghook_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _ghook_impl.install_ghook_from_cargo_binstall(_module(), bin_dir, version)


def _install_ghook_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _ghook_impl.install_ghook_from_cargo_install(_module(), bin_dir, version)


def _get_ghook_version_override() -> str | None:
    return _ghook_impl.get_ghook_version_override(_module())


def _get_ghook_method_override() -> str | None:
    return _ghook_impl.get_ghook_method_override(_module())


def _probe_ghook_version(ghook_path: Path) -> str | None:
    return _ghook_impl.probe_ghook_version(_module(), ghook_path)


def _install_ghook(force: bool = False) -> dict[str, Any]:
    return _ghook_impl.install_ghook(_module(), force)


_GWIKI_RELEASE_TAG_PREFIX = "gwiki-v"
_GWIKI_VERSION_STAMP = ".gwiki-version"
_GWIKI_BIN_NAME = "gwiki.exe" if sys.platform == "win32" else "gwiki"
_GWIKI_TARGETS = _PLATFORM_TARGETS
_GWIKI_CRATES_API = "https://crates.io/api/v1/crates/gobby-wiki"


def _get_latest_gwiki_version() -> str | None:
    return _gwiki_impl.get_latest_gwiki_version(_module())


def _get_installed_gwiki_version(bin_dir: Path) -> str | None:
    return _gwiki_impl.get_installed_gwiki_version(_module(), bin_dir)


def _write_gwiki_version_stamp(bin_dir: Path, version: str) -> None:
    _gwiki_impl.write_gwiki_version_stamp(_module(), bin_dir, version)


def _install_gwiki_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _gwiki_impl.install_gwiki_from_github(_module(), bin_dir, target, version)


def _install_gwiki_from_submodule(bin_dir: Path) -> bool:
    return _gwiki_impl.install_gwiki_from_submodule(_module(), bin_dir)


def _install_gwiki_from_cargo_git(bin_dir: Path, version: str | None = None) -> bool:
    return _gwiki_impl.install_gwiki_from_cargo_git(_module(), bin_dir, version)


def _install_gwiki_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _gwiki_impl.install_gwiki_from_cargo_binstall(_module(), bin_dir, version)


def _install_gwiki_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _gwiki_impl.install_gwiki_from_cargo_install(_module(), bin_dir, version)


def _probe_gwiki_version(gwiki_path: Path) -> str | None:
    return _gwiki_impl.probe_gwiki_version(_module(), gwiki_path)


def _install_gwiki(force: bool = False) -> dict[str, Any]:
    return _gwiki_impl.install_gwiki(_module(), force)


# Stage-0 gterm/gclient ships macOS/Linux only. Windows remains compile-only
# until the ConPTY milestone; the installer must not invent a Windows fallback.
_STAGE0_TARGETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
}

_GTERM_RELEASE_TAG_PREFIX = "gterm-v"
_GTERM_VERSION_STAMP = ".gterm-version"
_GTERM_BIN_NAME = "gterm"
_GTERM_TARGETS = _STAGE0_TARGETS
_GTERM_CRATES_API = "https://crates.io/api/v1/crates/gobby-terminal"


def _get_latest_gterm_version() -> str | None:
    return _gterm_impl.get_latest_gterm_version(_module())


def _get_installed_gterm_version(bin_dir: Path) -> str | None:
    return _gterm_impl.get_installed_gterm_version(_module(), bin_dir)


def _write_gterm_version_stamp(bin_dir: Path, version: str) -> None:
    _gterm_impl.write_gterm_version_stamp(_module(), bin_dir, version)


def _install_gterm_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _gterm_impl.install_gterm_from_github(_module(), bin_dir, target, version)


def _install_gterm_from_submodule(bin_dir: Path) -> bool:
    return _gterm_impl.install_gterm_from_submodule(_module(), bin_dir)


def _install_gterm_from_cargo_git(bin_dir: Path) -> bool:
    return _gterm_impl.install_gterm_from_cargo_git(_module(), bin_dir)


def _install_gterm_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _gterm_impl.install_gterm_from_cargo_binstall(_module(), bin_dir, version)


def _install_gterm_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _gterm_impl.install_gterm_from_cargo_install(_module(), bin_dir, version)


def _install_gterm(force: bool = False) -> dict[str, Any]:
    return _gterm_impl.install_gterm(_module(), force)


_GCLIENT_RELEASE_TAG_PREFIX = "gclient-v"
_GCLIENT_VERSION_STAMP = ".gclient-version"
_GCLIENT_BIN_NAME = "gclient"
_GCLIENT_TARGETS = _STAGE0_TARGETS
_GCLIENT_CRATES_API = "https://crates.io/api/v1/crates/gobby-client"


def _get_latest_gclient_version() -> str | None:
    return _gclient_impl.get_latest_gclient_version(_module())


def _get_installed_gclient_version(bin_dir: Path) -> str | None:
    return _gclient_impl.get_installed_gclient_version(_module(), bin_dir)


def _write_gclient_version_stamp(bin_dir: Path, version: str) -> None:
    _gclient_impl.write_gclient_version_stamp(_module(), bin_dir, version)


def _install_gclient_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _gclient_impl.install_gclient_from_github(_module(), bin_dir, target, version)


def _install_gclient_from_submodule(bin_dir: Path) -> bool:
    return _gclient_impl.install_gclient_from_submodule(_module(), bin_dir)


def _install_gclient_from_cargo_git(bin_dir: Path) -> bool:
    return _gclient_impl.install_gclient_from_cargo_git(_module(), bin_dir)


def _install_gclient_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _gclient_impl.install_gclient_from_cargo_binstall(_module(), bin_dir, version)


def _install_gclient_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _gclient_impl.install_gclient_from_cargo_install(_module(), bin_dir, version)


def _install_gclient(force: bool = False) -> dict[str, Any]:
    return _gclient_impl.install_gclient(_module(), force)
