"""RTK executable discovery and platform-native state paths."""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - direct argv probes of a user-installed executable
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

RTK_MINIMUM_VERSION = (0, 45, 0)
RTK_RULE_NAME = "rtk-command-rewrite"
RTK_VERSION = "0.45.0"

_VERSION_RE = re.compile(
    r"^rtk\s+(\d+)\.(\d+)\.(\d+)(?P<suffix>[-+][^\s]+)?$",
    re.IGNORECASE,
)
_PROBE_OUTPUT_LIMIT = 32 * 1024


@dataclass(frozen=True)
class RtkProbe:
    """Compatibility result for one RTK executable candidate."""

    path: Path
    version: str | None
    compatible: bool
    error: str | None = None


@dataclass(frozen=True)
class RtkPlatformPaths:
    """Platform-native RTK config and mutable data locations."""

    config_dir: Path
    data_dir: Path
    database_path: Path
    tee_dir: Path


@dataclass(frozen=True)
class RtkSandboxPaths:
    """Narrow filesystem grants needed by RTK inside a managed sandbox."""

    read_paths: tuple[Path, ...]
    write_paths: tuple[Path, ...]


def managed_rtk_path(*, home: Path | None = None) -> Path:
    """Return Gobby's fallback RTK binary path."""
    name = "rtk.exe" if os.name == "nt" else "rtk"
    return (home or Path.home()) / ".gobby" / "bin" / name


def platform_paths(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> RtkPlatformPaths:
    """Mirror the directories crate rules used by stock RTK."""
    values = os.environ if env is None else env
    user_home = home or Path.home()
    current_platform = sys.platform if platform is None else platform

    if current_platform == "darwin":
        config_root = user_home / "Library" / "Application Support"
        data_root = config_root
    elif current_platform.startswith("win"):
        config_root = Path(values.get("APPDATA", str(user_home / "AppData" / "Roaming")))
        data_root = Path(values.get("LOCALAPPDATA", str(user_home / "AppData" / "Local")))
    else:
        config_root = Path(values.get("XDG_CONFIG_HOME", str(user_home / ".config")))
        data_root = Path(values.get("XDG_DATA_HOME", str(user_home / ".local" / "share")))

    config_dir = config_root / "rtk"
    data_dir = data_root / "rtk"
    database_path = Path(values.get("RTK_DB_PATH", str(data_dir / "history.db")))
    tee_dir = Path(values.get("RTK_TEE_DIR", str(data_dir / "tee")))

    config_path = config_dir / "config.toml"
    if config_path.is_file():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            config = {}
        tracking = config.get("tracking")
        tee = config.get("tee")
        if "RTK_DB_PATH" not in values and isinstance(tracking, dict):
            configured_db = tracking.get("database_path")
            if isinstance(configured_db, str) and configured_db:
                database_path = Path(configured_db).expanduser()
        if "RTK_TEE_DIR" not in values and isinstance(tee, dict):
            configured_tee = tee.get("directory")
            if isinstance(configured_tee, str) and configured_tee:
                tee_dir = Path(configured_tee).expanduser()

    return RtkPlatformPaths(
        config_dir=config_dir,
        data_dir=data_dir,
        database_path=database_path,
        tee_dir=tee_dir,
    )


def _run_probe(argv: Sequence[str], *, timeout: float) -> tuple[int, bytes, bytes] | None:
    try:
        completed = subprocess.run(  # nosec B603 - executable is an explicit candidate path
            list(argv),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if len(completed.stdout) + len(completed.stderr) > _PROBE_OUTPUT_LIMIT:
        return None
    return completed.returncode, completed.stdout, completed.stderr


def probe_rtk(path: Path, *, timeout: float = 1.0) -> RtkProbe:
    """Verify RTK identity, minimum version, and hook-check CLI contract."""
    resolved = path.expanduser()
    version_result = _run_probe((str(resolved), "--version"), timeout=timeout)
    if version_result is None:
        return RtkProbe(resolved, None, False, "version probe failed")
    code, stdout, _stderr = version_result
    try:
        version_text = stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return RtkProbe(resolved, None, False, "version output is not UTF-8")
    match = _VERSION_RE.fullmatch(version_text)
    if code != 0 or match is None:
        return RtkProbe(resolved, None, False, "executable identity probe failed")
    version_parts = match.group(1, 2, 3)
    version_tuple = tuple(int(part) for part in version_parts)
    suffix = match.group("suffix") or ""
    version = ".".join(version_parts) + suffix
    if version_tuple < RTK_MINIMUM_VERSION or (
        version_tuple == RTK_MINIMUM_VERSION and suffix.startswith("-")
    ):
        return RtkProbe(resolved, version, False, "RTK 0.45.0 or newer is required")

    contract_result = _run_probe(
        (str(resolved), "hook", "check", "--help"),
        timeout=timeout,
    )
    if contract_result is None:
        return RtkProbe(resolved, version, False, "hook-check probe failed")
    hook_code, hook_stdout, hook_stderr = contract_result
    help_text = (hook_stdout + hook_stderr).decode("utf-8", errors="replace")
    if hook_code != 0 or "--agent" not in help_text or "Command to check" not in help_text:
        return RtkProbe(resolved, version, False, "hook-check contract is unavailable")
    return RtkProbe(resolved.resolve(), version, True)


def rtk_candidates(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Return deduplicated RTK candidates without trusting PATH identity."""
    values = os.environ if env is None else env
    candidates: list[Path] = []
    explicit = values.get("GOBBY_RTK_BIN")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(managed_rtk_path(home=home))
    from_path = shutil.which("rtk", path=values.get("PATH", ""))
    if from_path:
        candidates.append(Path(from_path))

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def resolve_rtk(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    timeout: float = 1.0,
) -> RtkProbe | None:
    """Resolve the first compatible RTK executable."""
    for candidate in rtk_candidates(env=env, home=home):
        if not candidate.expanduser().is_file():
            continue
        probe = probe_rtk(candidate, timeout=timeout)
        if probe.compatible:
            return probe
    return None


def sandbox_paths(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> RtkSandboxPaths | None:
    """Return RTK grants only when binary or user state exists."""
    values = os.environ if env is None else env
    paths = platform_paths(env=values, home=home, platform=platform)
    existing_candidates = [path.expanduser() for path in rtk_candidates(env=values, home=home)]
    has_state = paths.config_dir.exists() or paths.data_dir.exists()
    binaries = [path.resolve() for path in existing_candidates if path.is_file()]
    if not has_state and not binaries:
        return None

    read_paths = tuple(dict.fromkeys([*binaries, paths.config_dir, paths.data_dir]))
    write_paths = tuple(
        dict.fromkeys(
            [
                paths.data_dir,
                paths.database_path.parent,
                paths.tee_dir,
            ]
        )
    )
    return RtkSandboxPaths(read_paths=read_paths, write_paths=write_paths)
