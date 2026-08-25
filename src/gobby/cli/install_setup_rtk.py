"""Opt-in RTK provisioning and direct-hook reconciliation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess  # nosec B404 - direct argv package-manager invocation
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request

from gobby.integrations.rtk import (
    RTK_RULE_NAME,
    RTK_VERSION,
    RtkProbe,
    managed_rtk_path,
    probe_rtk,
    resolve_rtk,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase

from .install_setup import _extract_binary_from_release_archive, _urlopen_https

logger = logging.getLogger(__name__)

_RELEASE_BASE = f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}"
_MAX_RELEASE_BYTES = 64 * 1024 * 1024
_MANAGED_SIDECAR = ".rtk-gobby-install.json"
_RTK_BLOCK_START = "<!-- rtk-instructions"
_RTK_BLOCK_START_RE = re.compile(r"<!-- rtk-instructions(?: v\d+)? -->")
_RTK_BLOCK_END = "<!-- /rtk-instructions -->"
_KNOWN_RTK_FILES = {
    "fe21cb81e575985263598fd1b280a95faf0a89c97f757e9050844f9ec12def1e",
    "49c368c302c6f63d089f4c1085b242fc50fe22ce5bc34dada6478083000e7c6f",
}
_KNOWN_LEGACY_SCRIPT = "742418d70728fc3b24032fb8ae2c39a13169c2b93218beda23bfaf993d67bfe5"
_ASSET_CHECKSUMS = {
    "rtk-aarch64-apple-darwin.tar.gz": (
        "064151cfc2d50b24d810b06a0af2e41b9c945e83534e4c438c3d3eae607fc3f4"
    ),
    "rtk-x86_64-apple-darwin.tar.gz": (
        "9ea02f889d5a2779e4fb700df4587824303c5a57cda22e903e30058079fca0ef"
    ),
    "rtk-aarch64-unknown-linux-gnu.tar.gz": (
        "80a746dd305ef944ff50ef011ae4ce3878dd5ba88dfe35d859d05498191637c3"
    ),
    "rtk-x86_64-unknown-linux-musl.tar.gz": (
        "c4c036fbf181fc55ef329786c8c17e0d427972b053b825944d968a6aafef1ba4"
    ),
    "rtk-x86_64-pc-windows-msvc.zip": (
        "34cea9009a8099acdaf85147b971d95f65efabfa63fb3aea7d3e2b73e6f517c3"
    ),
}


class RtkInstallError(RuntimeError):
    """Raised when consented RTK provisioning cannot produce a compatible binary."""


@dataclass(frozen=True)
class RtkCleanupReport:
    """Exact artifacts removed plus ambiguous content retained for review."""

    removed: tuple[Path, ...]
    backups: tuple[Path, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class RtkInstallStatus:
    """User-visible RTK integration state."""

    binary_path: Path | None
    version: str | None
    rule_enabled: bool
    direct_artifact_conflicts: tuple[str, ...]
    health: str
    managed_binary: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.gobby-rtk.bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.gobby-rtk.bak.{counter}")
        counter += 1
    shutil.copy2(path, candidate)
    return candidate


def _asset_for_platform() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if system == "darwin":
        target = "aarch64-apple-darwin" if arm else "x86_64-apple-darwin"
        return f"rtk-{target}.tar.gz", ".tar.gz", "rtk"
    if system == "linux":
        target = "aarch64-unknown-linux-gnu" if arm else "x86_64-unknown-linux-musl"
        return f"rtk-{target}.tar.gz", ".tar.gz", "rtk"
    if system == "windows" and not arm:
        return "rtk-x86_64-pc-windows-msvc.zip", ".zip", "rtk.exe"
    raise RtkInstallError(f"RTK fallback is unavailable for {system}/{machine}")


def _download_fallback(*, home: Path | None = None) -> RtkProbe:
    asset, archive_ext, binary_name = _asset_for_platform()
    request = Request(
        f"{_RELEASE_BASE}/{asset}",
        headers={"User-Agent": "gobby-installer/1.0"},
    )
    try:
        with _urlopen_https(request, timeout=30) as response:
            archive = response.read(_MAX_RELEASE_BYTES + 1)
    except (OSError, URLError) as exc:
        raise RtkInstallError(f"RTK fallback download failed: {exc}") from exc
    if len(archive) > _MAX_RELEASE_BYTES:
        raise RtkInstallError("RTK fallback archive exceeded the download limit")
    expected = _ASSET_CHECKSUMS[asset]
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected:
        raise RtkInstallError(f"RTK fallback checksum mismatch for {asset}")

    target = managed_rtk_path(home=home)
    if not _extract_binary_from_release_archive(
        archive,
        archive_ext=archive_ext,
        binary_name=binary_name,
        bin_dir=target.parent,
        label=asset,
    ):
        raise RtkInstallError("RTK fallback archive could not be installed")
    probe = probe_rtk(target)
    if not probe.compatible:
        raise RtkInstallError(probe.error or "installed RTK fallback failed verification")
    sidecar = target.parent / _MANAGED_SIDECAR
    sidecar.write_text(
        json.dumps(
            {
                "path": str(target),
                "sha256": _sha256(target),
                "version": probe.version,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return probe


def _brew_probe(brew: str) -> RtkProbe | None:
    try:
        prefix = subprocess.run(  # nosec B603 - direct Homebrew argv
            [brew, "--prefix", "rtk"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if prefix.returncode != 0:
        return None
    candidate = Path(prefix.stdout.strip()) / "bin" / "rtk"
    if not candidate.is_file():
        return None
    probe = probe_rtk(candidate)
    return probe if probe.compatible else None


def ensure_rtk(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> RtkProbe:
    """Return compatible RTK, provisioning only after caller consent."""
    values = os.environ if env is None else env
    existing = resolve_rtk(env=values, home=home)
    if existing is not None:
        return existing

    brew = shutil.which("brew", path=values.get("PATH", ""))
    if brew:
        try:
            installed = subprocess.run(  # nosec B603 - direct Homebrew argv
                [brew, "install", "rtk"],
                capture_output=True,
                check=False,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Homebrew RTK provisioning failed: %s", exc)
        else:
            if installed.returncode == 0:
                brewed = _brew_probe(brew)
                if brewed is not None:
                    return brewed
            logger.warning("Homebrew did not provide a compatible RTK executable")

    return _download_fallback(home=home)


def _is_exact_hook_command(command: str, *, legacy_scripts: set[Path]) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) == 3 and Path(parts[0]).name in {"rtk", "rtk.exe"}:
        return parts[1] == "hook" and parts[2] in {
            "claude",
            "codex",
            "qwen",
            "grok",
            "droid",
            "gemini",
            "agy",
        }
    script_parts = parts[1:] if parts and Path(parts[0]).name in {"bash", "sh"} else parts
    if len(script_parts) != 1:
        return False
    script = Path(script_parts[0]).expanduser()
    return any(
        script == known or script.resolve(strict=False) == known.resolve(strict=False)
        for known in legacy_scripts
    )


def _scrub_hook_value(
    value: Any,
    *,
    legacy_scripts: set[Path],
    conflicts: list[str],
    source: Path,
) -> tuple[Any, bool]:
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str):
            if _is_exact_hook_command(command, legacy_scripts=legacy_scripts):
                return None, True
            if "rtk" in command.lower():
                conflicts.append(f"{source}: ambiguous RTK hook command {command!r}")
        changed = False
        result: dict[str, Any] = {}
        for key, child in value.items():
            scrubbed, child_changed = _scrub_hook_value(
                child,
                legacy_scripts=legacy_scripts,
                conflicts=conflicts,
                source=source,
            )
            changed = changed or child_changed
            if scrubbed is not None:
                result[key] = scrubbed
        if result.get("hooks") == [] and set(result) <= {"matcher", "hooks"}:
            return None, True
        return result, changed
    if isinstance(value, list):
        result_list: list[Any] = []
        changed = False
        for child in value:
            scrubbed, child_changed = _scrub_hook_value(
                child,
                legacy_scripts=legacy_scripts,
                conflicts=conflicts,
                source=source,
            )
            changed = changed or child_changed
            if scrubbed is not None:
                result_list.append(scrubbed)
        return result_list, changed
    return value, False


def _remove_instruction_content(content: str) -> tuple[str, bool, bool]:
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_block = False
    changed = False
    malformed = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_RTK_BLOCK_START):
            if _RTK_BLOCK_START_RE.fullmatch(stripped) is None:
                malformed = True
            if in_block:
                malformed = True
            in_block = True
            changed = True
            continue
        if stripped == _RTK_BLOCK_END:
            if not in_block:
                malformed = True
            in_block = False
            changed = True
            continue
        if in_block:
            continue
        if stripped == "@RTK.md":
            changed = True
            continue
        result.append(line)
    if in_block:
        malformed = True
    if malformed:
        return content, False, True
    return "".join(result), changed, False


def reconcile_direct_artifacts(
    *,
    home: Path | None = None,
    remove: bool,
) -> RtkCleanupReport:
    """Remove only exact RTK-generated CLI artifacts, backing up every edit."""
    user_home = home or Path.home()
    config_paths = (
        user_home / ".claude" / "settings.json",
        user_home / ".qwen" / "settings.json",
        user_home / ".codex" / "hooks.json",
        user_home / ".grok" / "hooks" / "rtk.json",
        user_home / ".grok" / "hooks" / "gobby.json",
        user_home / ".factory" / "settings.json",
        user_home / ".factory" / "hooks" / "hooks.json",
        user_home / ".gemini" / "config" / "hooks.json",
    )
    legacy_scripts = {
        user_home / ".claude" / "hooks" / "rtk-rewrite.sh",
        user_home / ".qwen" / "hooks" / "rtk-rewrite.sh",
        user_home / ".factory" / "hooks" / "rtk-rewrite.sh",
    }
    instruction_paths = (
        user_home / ".claude" / "CLAUDE.md",
        user_home / ".qwen" / "QWEN.md",
        user_home / ".codex" / "AGENTS.md",
        user_home / ".grok" / "AGENTS.md",
        user_home / ".factory" / "AGENTS.md",
        user_home / ".gemini" / "GEMINI.md",
    )
    generated_files = (
        user_home / ".claude" / "RTK.md",
        user_home / ".qwen" / "RTK.md",
        user_home / ".codex" / "RTK.md",
        user_home / ".grok" / "RTK.md",
        user_home / ".factory" / "RTK.md",
        user_home / ".gemini" / "RTK.md",
    )
    removed: list[Path] = []
    backups: list[Path] = []
    conflicts: list[str] = []

    for path in config_paths:
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            conflicts.append(f"{path}: could not inspect RTK hooks: {exc}")
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            if "rtk" in raw.lower():
                conflicts.append(f"{path}: could not inspect RTK hooks: {exc}")
            continue
        scrubbed, changed = _scrub_hook_value(
            parsed,
            legacy_scripts=legacy_scripts,
            conflicts=conflicts,
            source=path,
        )
        if not changed:
            continue
        if not remove:
            conflicts.append(f"{path}: direct RTK hook remains installed")
            continue
        backup = _backup(path)
        path.write_text(json.dumps(scrubbed, indent=2) + "\n", encoding="utf-8")
        backups.append(backup)
        removed.append(path)

    for path in instruction_paths:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            conflicts.append(f"{path}: could not inspect RTK instructions: {exc}")
            continue
        cleaned, changed, malformed = _remove_instruction_content(content)
        if malformed:
            conflicts.append(f"{path}: malformed RTK instruction block preserved")
        elif changed and not remove:
            conflicts.append(f"{path}: generated RTK instructions remain installed")
        elif changed:
            backup = _backup(path)
            path.write_text(cleaned, encoding="utf-8")
            backups.append(backup)
            removed.append(path)

    for path in generated_files:
        if not path.is_file():
            continue
        if _sha256(path) not in _KNOWN_RTK_FILES:
            conflicts.append(f"{path}: modified RTK.md preserved")
        elif not remove:
            conflicts.append(f"{path}: generated RTK.md remains installed")
        else:
            backup = _backup(path)
            path.unlink()
            backups.append(backup)
            removed.append(path)

    for path in legacy_scripts:
        if not path.is_file():
            continue
        if _sha256(path) != _KNOWN_LEGACY_SCRIPT:
            conflicts.append(f"{path}: modified legacy RTK script preserved")
        elif not remove:
            conflicts.append(f"{path}: legacy RTK script remains installed")
        else:
            backup = _backup(path)
            path.unlink()
            backups.append(backup)
            removed.append(path)

    return RtkCleanupReport(
        removed=tuple(removed),
        backups=tuple(backups),
        conflicts=tuple(dict.fromkeys(conflicts)),
    )


def rule_state(db: HubDatabase) -> bool | None:
    row = RuleDefinitionManager(db).get_by_name(RTK_RULE_NAME, project_id=None)
    return row.enabled if row is not None else None


def set_rule_state(db: HubDatabase, *, enabled: bool) -> bool:
    manager = RuleDefinitionManager(db)
    row = manager.get_by_name(RTK_RULE_NAME, project_id=None)
    if row is None:
        raise RtkInstallError(
            f"Bundled rule {RTK_RULE_NAME!r} is unavailable; run daemon setup first"
        )
    if row.enabled != enabled:
        row = manager.update(row.id, enabled=enabled)
    return row.enabled


def disable_rule_if_present(db: HubDatabase) -> bool:
    """Disable the installed global rule and tolerate a never-synced template."""
    current = rule_state(db)
    if current is None:
        return False
    set_rule_state(db, enabled=False)
    return True


def resolve_selection(
    explicit: bool | None,
    *,
    no_interactive: bool,
    current: bool | None,
    confirm: Callable[..., bool],
) -> bool:
    if explicit is not None:
        return explicit
    if no_interactive:
        return current is True
    return confirm(
        "Enable RTK command rewriting through Gobby?",
        default=current is True,
    )


def get_rtk_status(
    db: HubDatabase,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> RtkInstallStatus:
    probe = resolve_rtk(home=home, env=env)
    enabled = rule_state(db) is True
    conflicts = reconcile_direct_artifacts(home=home, remove=False).conflicts
    if not enabled:
        health = "disabled"
    elif probe is None:
        health = "unavailable"
    elif conflicts:
        health = "conflicted"
    else:
        health = "healthy"
    target = managed_rtk_path(home=home)
    sidecar = target.parent / _MANAGED_SIDECAR
    managed = False
    if probe is not None and probe.path == target.resolve() and sidecar.is_file():
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            managed = (
                metadata.get("path") == str(target)
                and target.is_file()
                and metadata.get("sha256") == _sha256(target)
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            managed = False
    return RtkInstallStatus(
        binary_path=probe.path if probe else None,
        version=probe.version if probe else None,
        rule_enabled=enabled,
        direct_artifact_conflicts=conflicts,
        health=health,
        managed_binary=managed,
    )


def reconcile_rtk(
    db: HubDatabase,
    explicit: bool | None,
    *,
    no_interactive: bool,
    confirm: Callable[..., bool],
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> RtkInstallStatus:
    """Apply tri-state consent, provision RTK, clean direct hooks, and toggle rule."""
    desired = resolve_selection(
        explicit,
        no_interactive=no_interactive,
        current=rule_state(db),
        confirm=confirm,
    )
    if desired:
        ensure_rtk(env=env, home=home)
        cleanup = reconcile_direct_artifacts(home=home, remove=True)
        for conflict in cleanup.conflicts:
            logger.warning("RTK artifact conflict: %s", conflict)
    set_rule_state(db, enabled=desired)
    return get_rtk_status(db, home=home, env=env)


def remove_managed_rtk(*, home: Path | None = None) -> RtkCleanupReport:
    """Remove only the checksum-matching fallback binary owned by Gobby."""
    target = managed_rtk_path(home=home)
    sidecar = target.parent / _MANAGED_SIDECAR
    if not sidecar.is_file():
        return RtkCleanupReport(removed=(), backups=(), conflicts=())
    try:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return RtkCleanupReport(
            removed=(),
            backups=(),
            conflicts=(f"{sidecar}: invalid Gobby ownership metadata: {exc}",),
        )
    if metadata.get("path") != str(target):
        return RtkCleanupReport(
            removed=(),
            backups=(),
            conflicts=(f"{sidecar}: managed RTK path does not match {target}",),
        )
    if target.is_file() and metadata.get("sha256") != _sha256(target):
        return RtkCleanupReport(
            removed=(),
            backups=(),
            conflicts=(f"{target}: managed RTK binary was modified and was preserved",),
        )
    removed: list[Path] = []
    if target.exists():
        target.unlink()
        removed.append(target)
    sidecar.unlink()
    removed.append(sidecar)
    return RtkCleanupReport(removed=tuple(removed), backups=(), conflicts=())
