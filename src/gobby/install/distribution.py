"""Distribution-specific install behavior."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 # hardcoded helper version probes
from dataclasses import dataclass

from gobby.install.bin_freshness_models import is_at_least_version
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.install.version_probe import probe_native_bin_version
from gobby.utils.native_bin import is_native_bin_usable, local_native_bin_path, native_bin_name

HOMEBREW_DISTRIBUTION = "homebrew"
GOBBY_DISTRIBUTION_ENV = "GOBBY_DISTRIBUTION"

HOMEBREW_HELPERS: tuple[str, ...] = ("gcode", "ghook", "gwiki", "gterm", "gclient")
HOMEBREW_HELPER_FORMULAE: dict[str, str] = {
    "gcode": "gobby-code",
    "ghook": "gobby-hooks",
    "gwiki": "gobby-wiki",
    "gterm": "gobby-terminal",
    "gclient": "gobby-client",
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
    """Verify helper binaries satisfy pinned minimums for Homebrew distribution."""
    statuses = [_inspect_homebrew_helper(name) for name in HOMEBREW_HELPERS]
    failures = [status for status in statuses if not status.ok]
    if failures:
        raise HomebrewDistributionError(_format_homebrew_helper_failures(failures))
    return statuses


def _inspect_homebrew_helper(name: str) -> HomebrewHelperStatus:
    formula = HOMEBREW_HELPER_FORMULAE[name]
    minimum = MANAGED_BIN_VERSION_PINS[name]
    local_status = _inspect_local_managed_helper(name, formula, minimum)
    if local_status is not None:
        return local_status

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

    if not is_at_least_version(version, minimum):
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


def _inspect_local_managed_helper(
    name: str,
    formula: str,
    minimum: str,
) -> HomebrewHelperStatus | None:
    path = local_native_bin_path(name)
    if not is_native_bin_usable(path):
        return None

    version = _probe_helper_version(str(path))
    if version is None or not is_at_least_version(version, minimum):
        return None

    return HomebrewHelperStatus(
        name=name,
        formula=formula,
        minimum_version=minimum,
        path=str(path),
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
    return probe_native_bin_version(path, runner=subprocess.run)


def _format_homebrew_helper_failures(failures: list[HomebrewHelperStatus]) -> str:
    sections = ["Homebrew-managed Gobby requires helper binaries satisfying pinned floors."]
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
