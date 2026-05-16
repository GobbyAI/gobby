"""Helpers for install-time daemon bootstrap and bundled binary installs."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from shutil import copy2
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click

from gobby.install.distribution import (
    HomebrewDistributionError,
    is_homebrew_distribution,
    verify_homebrew_managed_bins,
)

from . import install_setup_gcode as _gcode_impl
from . import install_setup_ghook as _ghook_impl
from . import install_setup_gloc as _gloc_impl
from . import install_setup_gsqz as _gsqz_impl
from .utils import get_install_dir

logger = logging.getLogger(__name__)
# Helper modules resolve these names dynamically from this module to preserve
# existing patch targets in tests and callers.
_HELPER_EXPORTS = (os, platform, tempfile, UTC, datetime)


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
    return f"https://github.com/GobbyAI/gobby-cli/releases/download/{tag_name}/{artifact_filename}"


def _parse_release_semver(version_text: str) -> tuple[int, ...] | None:
    """Parse a simple semver string into a sortable tuple when possible."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?", version_text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def _resolve_latest_release_tag(*, tag_prefix: str) -> str:
    """Resolve the newest stable GitHub release tag for a tag prefix."""
    req = Request(
        "https://api.github.com/repos/GobbyAI/gobby-cli/releases?per_page=100",
        headers={
            "User-Agent": "gobby-installer/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with _urlopen_https(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode("utf-8"))

    if not isinstance(releases, list):
        raise ValueError("GitHub Releases API returned an unexpected payload")

    stable_matches: list[tuple[str, str]] = []
    semver_matches: list[tuple[tuple[int, ...], str, str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
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
    raise ValueError(f"No matching stable release found for tag prefix {tag_prefix!r}")


def _extract_binary_from_release_archive(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    bin_dir: Path,
    label: str,
) -> bool:
    """Extract one binary from a release archive into ``bin_dir``."""
    dest = bin_dir / binary_name
    bin_dir.mkdir(parents=True, exist_ok=True)

    try:
        if archive_ext == "zip":
            with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
                for member_name in archive.namelist():
                    if member_name.endswith(f"/{binary_name}") or member_name == binary_name:
                        with archive.open(member_name) as fileobj:
                            dest.write_bytes(fileobj.read())
                        dest.chmod(0o755)
                        return True
        else:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.name.endswith(f"/{binary_name}") or member.name == binary_name:
                        extracted_file = archive.extractfile(member)
                        if extracted_file is None:
                            continue
                        dest.write_bytes(extracted_file.read())
                        dest.chmod(0o755)
                        return True
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as e:
        logger.warning("%s: failed extracting release archive: %s", label, e)
        return False

    logger.warning("%s binary not found in release archive", label)
    return False


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


def ensure_daemon_config() -> dict[str, Any]:
    """Ensure bootstrap config exists at ~/.gobby/bootstrap.yaml.

    If bootstrap.yaml doesn't exist, copies the shared template.
    Bootstrap.yaml contains only the 5 pre-DB settings; all other
    configuration is managed via the DB (config_store) + Pydantic defaults.

    Returns:
        Dict with 'created' (bool) and 'path' (str) keys
    """
    bootstrap_path = Path("~/.gobby/bootstrap.yaml").expanduser()

    if bootstrap_path.exists():
        return {"created": False, "path": str(bootstrap_path)}

    bootstrap_path.parent.mkdir(parents=True, exist_ok=True)

    shared_bootstrap = get_install_dir() / "shared" / "config" / "bootstrap.yaml"
    if shared_bootstrap.exists():
        copy2(shared_bootstrap, bootstrap_path)
        bootstrap_path.chmod(0o600)
        return {"created": True, "path": str(bootstrap_path), "source": "shared"}

    import yaml

    defaults = {
        "database_path": "~/.gobby/gobby-hub.db",
        "daemon_port": 60887,
        "bind_host": "localhost",
        "websocket_port": 60888,
        "ui_port": 60889,
    }
    with open(bootstrap_path, "w") as f:
        yaml.safe_dump(defaults, f, default_flow_style=False, sort_keys=False)
    bootstrap_path.chmod(0o600)
    return {"created": True, "path": str(bootstrap_path), "source": "generated"}


def run_daemon_setup(project_path: Path) -> None:
    """Run install setup: DB init, bundled content sync, MCP servers, IDE config.

    Called after ensure_daemon_config(). Handles database initialization,
    bundled content sync, default MCP server installation, and IDE config.

    Args:
        project_path: The project directory path (used for context only).
    """
    from .installers import install_default_mcp_servers

    db = None
    try:
        from gobby.cli.utils import init_local_storage

        db = init_local_storage()
        click.echo("Database initialized")
    except (OSError, PermissionError, ValueError) as e:
        click.echo(f"Warning: Database init failed ({type(e).__name__}): {e}")

    if db is not None:
        try:
            from gobby.cli.installers.shared import sync_bundled_content_to_db

            sync_result = sync_bundled_content_to_db(db)
            if sync_result["total_synced"] > 0:
                click.echo(f"Synced {sync_result['total_synced']} bundled items to database")
            if sync_result["errors"]:
                for err in sync_result["errors"]:
                    click.echo(f"  Warning: {err}")
        finally:
            db.close()

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

    homebrew_mode = is_homebrew_distribution()
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
        from .installers.ide_config import configure_ide_terminal_title

        vscode_result = configure_ide_terminal_title("Code")
        if vscode_result.get("added"):
            click.echo("Configured VS Code terminal title for tmux integration")
    except (ImportError, OSError, PermissionError, ValueError) as e:
        click.echo(f"Warning: Failed to configure VS Code terminal title: {e}")


def _run_npm_install(label: str, package: str, project_path: Path) -> None:
    try:
        npm_result = subprocess.run(
            ["npm", "install", "-g", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if npm_result.returncode == 0:
            click.echo(f"Installed {label} ({package.removesuffix('@latest')})")
            if package == "@playwright/cli@latest":
                _remove_legacy_playwright_skill(project_path)
        else:
            click.echo(f"Warning: Failed to install {label}: {npm_result.stderr.strip()}")
    except FileNotFoundError:
        click.echo(f"Warning: npm not found — skipping {label} install")
    except subprocess.TimeoutExpired:
        click.echo(f"Warning: {label} install timed out")


def _remove_legacy_playwright_skill(project_path: Path) -> None:
    legacy_skills = project_path / ".claude" / "skills" / "playwright-cli"
    if not legacy_skills.exists():
        return
    try:
        shutil.rmtree(legacy_skills)
        click.echo("Removed legacy .claude/skills/playwright-cli/ (now in gobby-skills)")
    except OSError as e:
        click.secho(f"Warning: Could not remove legacy {legacy_skills}: {e}", fg="yellow")


def _run_managed_native_binary_installs() -> None:
    try:
        gsqz_result = _install_gsqz()
        if gsqz_result.get("installed"):
            verb = "Upgraded" if gsqz_result.get("upgraded") else "Installed"
            click.echo(
                f"{verb} gsqz {gsqz_result.get('version', '')} "
                f"via {gsqz_result.get('method', 'unknown')} (output compressor)"
            )
        elif gsqz_result.get("skipped"):
            reason = gsqz_result.get("reason", "")
            suffix = f" ({reason})" if reason else ""
            click.echo(f"gsqz already installed and up to date{suffix}")
        else:
            reason = gsqz_result.get("reason", "unknown error")
            click.echo(f"Warning: Failed to install gsqz: {reason}")
    except Exception as e:
        click.echo(f"Warning: Failed to install gsqz: {e}")

    try:
        gcode_result = _install_gcode()
        if gcode_result.get("installed"):
            verb = "Upgraded" if gcode_result.get("upgraded") else "Installed"
            click.echo(
                f"{verb} gcode {gcode_result.get('version', '')} "
                f"via {gcode_result.get('method', 'unknown')} (code index CLI)"
            )
        elif gcode_result.get("skipped"):
            reason = gcode_result.get("reason", "")
            suffix = f" ({reason})" if reason else ""
            click.echo(f"gcode already installed and up to date{suffix}")
        else:
            reason = gcode_result.get("reason", "unknown error")
            click.echo(f"Warning: Failed to install gcode: {reason}")
    except Exception as e:
        click.echo(f"Warning: Failed to install gcode: {e}")

    try:
        ghook_result = _install_ghook()
        if ghook_result.get("installed"):
            verb = "Upgraded" if ghook_result.get("upgraded") else "Installed"
            click.echo(
                f"{verb} ghook {ghook_result.get('version', '')} "
                f"via {ghook_result.get('method', 'unknown')} (hook manager)"
            )
        elif ghook_result.get("skipped"):
            reason = ghook_result.get("reason", "")
            suffix = f" ({reason})" if reason else ""
            click.echo(f"ghook already installed and up to date{suffix}")
        else:
            reason = ghook_result.get("reason", "unknown error")
            click.echo(f"Warning: Failed to install ghook: {reason}")
    except Exception as e:
        click.echo(f"Warning: Failed to install ghook: {e}")

    try:
        gloc_result = _install_gloc()
        if gloc_result.get("installed"):
            verb = "Upgraded" if gloc_result.get("upgraded") else "Installed"
            click.echo(
                f"{verb} gloc {gloc_result.get('version', '')} "
                f"via {gloc_result.get('method', 'unknown')} (local LLM launcher)"
            )
        elif gloc_result.get("skipped"):
            reason = gloc_result.get("reason", "")
            suffix = f" ({reason})" if reason else ""
            click.echo(f"gloc already installed and up to date{suffix}")
        else:
            reason = gloc_result.get("reason", "unknown error")
            click.echo(f"Warning: Failed to install gloc: {reason}")
    except Exception as e:
        click.echo(f"Warning: Failed to install gloc: {e}")


_GSQZ_RELEASE_TAG_PREFIX = "gsqz-v"
_GSQZ_CRATES_API = "https://crates.io/api/v1/crates/gobby-squeeze"
_GSQZ_VERSION_STAMP = ".gsqz-version"
_GSQZ_BIN_NAME = "gsqz.exe" if sys.platform == "win32" else "gsqz"

# Platform -> target triple mapping used across public binary installers.
_PLATFORM_TARGETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("win32", "amd64"): "x86_64-pc-windows-msvc",
    ("win32", "arm64"): "aarch64-pc-windows-msvc",
}


def _get_latest_gsqz_version() -> str | None:
    return _gsqz_impl.get_latest_gsqz_version(_module())


def _get_installed_gsqz_version(bin_dir: Path) -> str | None:
    return _gsqz_impl.get_installed_gsqz_version(_module(), bin_dir)


def _write_gsqz_version_stamp(bin_dir: Path, version: str) -> None:
    _gsqz_impl.write_gsqz_version_stamp(_module(), bin_dir, version)


def _install_gsqz_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _gsqz_impl.install_gsqz_from_github(_module(), bin_dir, target, version)


def _install_gsqz_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _gsqz_impl.install_gsqz_from_cargo_binstall(_module(), bin_dir, version)


def _install_gsqz_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _gsqz_impl.install_gsqz_from_cargo_install(_module(), bin_dir, version)


def _ensure_gobby_bin_on_path() -> dict[str, Any]:
    return _gsqz_impl.ensure_gobby_bin_on_path(_module())


def _install_gsqz(force: bool = False) -> dict[str, Any]:
    return _gsqz_impl.install_gsqz(_module(), force)


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
_GHOOK_COMPATIBILITY_STAMP = ".ghook-compatibility"
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


_GLOC_RELEASE_TAG_PREFIX = "gloc-v"
_GLOC_CRATES_API = "https://crates.io/api/v1/crates/gobby-local"
_GLOC_VERSION_STAMP = ".gloc-version"
_GLOC_BIN_NAME = "gloc.exe" if sys.platform == "win32" else "gloc"
_GLOC_TARGETS = _PLATFORM_TARGETS


def _get_latest_gloc_version() -> str | None:
    return _gloc_impl.get_latest_gloc_version(_module())


def _get_installed_gloc_version(bin_dir: Path) -> str | None:
    return _gloc_impl.get_installed_gloc_version(_module(), bin_dir)


def _write_gloc_version_stamp(bin_dir: Path, version: str) -> None:
    _gloc_impl.write_gloc_version_stamp(_module(), bin_dir, version)


def _install_gloc_from_github(bin_dir: Path, target: str, version: str | None = None) -> bool:
    return _gloc_impl.install_gloc_from_github(_module(), bin_dir, target, version)


def _install_gloc_from_cargo_binstall(bin_dir: Path, version: str | None = None) -> bool:
    return _gloc_impl.install_gloc_from_cargo_binstall(_module(), bin_dir, version)


def _install_gloc_from_cargo_install(bin_dir: Path, version: str | None = None) -> bool:
    return _gloc_impl.install_gloc_from_cargo_install(_module(), bin_dir, version)


def _probe_gloc_version(gloc_path: Path) -> str | None:
    return _gloc_impl.probe_gloc_version(_module(), gloc_path)


def _install_gloc(force: bool = False) -> dict[str, Any]:
    return _gloc_impl.install_gloc(_module(), force)
