"""Distribution-specific install behavior."""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 # hardcoded helper version probes
from dataclasses import dataclass

from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.utils.native_bin import native_bin_name

HOMEBREW_DISTRIBUTION = "homebrew"
GOBBY_DISTRIBUTION_ENV = "GOBBY_DISTRIBUTION"

HOMEBREW_HELPERS: tuple[str, ...] = ("gcode", "gsqz", "ghook", "gloc")
HOMEBREW_HELPER_FORMULAE: dict[str, str] = {
    "gcode": "gobby-code",
    "gsqz": "gobby-squeeze",
    "ghook": "gobby-hooks",
    "gloc": "gobby-local",
}


@dataclass(frozen=True)
class HomebrewHelperStatus:
    """Observed state for one Homebrew-managed helper binary."""

    name: str
    formula: str
    minimum_version: str
    path: str | None
    version: str | None
    ok: bool
    reason: str | None = None


class HomebrewDistributionError(RuntimeError):
    """Raised when Homebrew-managed dependencies are missing or stale."""


def is_homebrew_distribution() -> bool:
    """Return true when Gobby is running from the Homebrew formula wrapper."""
    return os.environ.get(GOBBY_DISTRIBUTION_ENV, "").strip().lower() == HOMEBREW_DISTRIBUTION


def verify_homebrew_managed_bins() -> list[HomebrewHelperStatus]:
    """Verify Homebrew helper binaries are on PATH and satisfy pinned minimums."""
    statuses = [_inspect_homebrew_helper(name) for name in HOMEBREW_HELPERS]
    failures = [status for status in statuses if not status.ok]
    if failures:
        raise HomebrewDistributionError(_format_homebrew_helper_failures(failures))
    return statuses


def _inspect_homebrew_helper(name: str) -> HomebrewHelperStatus:
    formula = HOMEBREW_HELPER_FORMULAE[name]
    minimum = MANAGED_BIN_VERSION_PINS[name]
    path = _which_path_only(name)
    if path is None:
        return HomebrewHelperStatus(
            name=name,
            formula=formula,
            minimum_version=minimum,
            path=None,
            version=None,
            ok=False,
            reason="missing",
        )

    version = _probe_helper_version(path)
    if version is None:
        return HomebrewHelperStatus(
            name=name,
            formula=formula,
            minimum_version=minimum,
            path=path,
            version=None,
            ok=False,
            reason="unversioned",
        )

    if not _version_at_least(version, minimum):
        return HomebrewHelperStatus(
            name=name,
            formula=formula,
            minimum_version=minimum,
            path=path,
            version=version,
            ok=False,
            reason="stale",
        )

    return HomebrewHelperStatus(
        name=name,
        formula=formula,
        minimum_version=minimum,
        path=path,
        version=version,
        ok=True,
    )


def _which_path_only(name: str) -> str | None:
    executable = native_bin_name(name)
    path = shutil.which(executable)
    if path:
        return path
    if executable != name:
        return shutil.which(name)
    return None


def _probe_helper_version(path: str) -> str | None:
    try:
        result = subprocess.run(  # nosec B603 # path comes from PATH lookup
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    output = (result.stdout or result.stderr).strip()
    match = re.search(r"\bv?(\d+(?:\.\d+){1,3})(?:[-+][^\s]+)?\b", output)
    return match.group(1) if match else None


def _version_at_least(version: str, minimum: str) -> bool:
    current = _version_tuple(version)
    floor = _version_tuple(minimum)
    if current is None or floor is None:
        return False
    return current >= floor


def _version_tuple(version: str) -> tuple[int, int, int, int] | None:
    match = re.match(r"v?(\d+(?:\.\d+){0,3})", version.strip())
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 4:
        parts.append(0)
    return (parts[0], parts[1], parts[2], parts[3])


def _format_homebrew_helper_failures(failures: list[HomebrewHelperStatus]) -> str:
    sections = ["Homebrew-managed Gobby requires Brew-installed helper binaries on PATH."]
    for failure in failures:
        sections.append(_format_homebrew_helper_failure(failure))
    return "\n\n".join(sections)


def _format_homebrew_helper_failure(failure: HomebrewHelperStatus) -> str:
    requirement = f"{failure.name} >= {failure.minimum_version}"
    if failure.reason == "missing":
        observed = f"{failure.name} was not found on PATH"
    elif failure.reason == "unversioned":
        observed = f"{failure.name} at {failure.path} did not report a version"
    else:
        observed = f"{failure.name} {failure.version} at {failure.path} is too old"

    return "\n".join(
        [
            f"{requirement} required; {observed}.",
            "Install or upgrade with Homebrew:",
            f"  brew install GobbyAI/tap/{failure.formula}",
            f"  brew upgrade GobbyAI/tap/{failure.formula}",
        ]
    )


__all__ = [
    "GOBBY_DISTRIBUTION_ENV",
    "HOMEBREW_DISTRIBUTION",
    "HOMEBREW_HELPER_FORMULAE",
    "HOMEBREW_HELPERS",
    "HomebrewDistributionError",
    "HomebrewHelperStatus",
    "is_homebrew_distribution",
    "verify_homebrew_managed_bins",
]
