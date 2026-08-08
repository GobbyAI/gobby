"""Shared dependency contracts for install, start, status, and managed SRT."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess  # Fixed dependency version commands. # nosec B404
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

DependencyState = Literal["healthy", "missing", "outdated", "invalid"]
DependencyPayload = dict[str, str | None]

PYTHON_MIN_VERSION = "3.13.0"
TMUX_MIN_VERSION = "3.2"
GIT_MIN_VERSION = "2.38.0"
NODE_MIN_VERSION = "20.11.0"
DOCKER_COMPOSE_MIN_VERSION = "2.7.0"
STARTING_GRACE_SECONDS = 120.0

_VERSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])v?(?P<numeric>\d+(?:\.\d+){1,3})"
    r"(?P<suffix>(?:[-+]?[A-Za-z][A-Za-z0-9.-]*)?)"
)


@dataclass(frozen=True)
class SrtRelease:
    """Immutable integrity contract for Gobby's managed Sandbox Runtime."""

    package: str
    version: str
    tarball_url: str
    tarball_sha256: str
    npm_integrity: str
    lockfile_sha256: str
    runner_sha256: str

    def receipt_fields(self) -> dict[str, str]:
        return {
            "package": self.package,
            "version": self.version,
            "tarball_url": self.tarball_url,
            "tarball_sha256": self.tarball_sha256,
            "npm_integrity": self.npm_integrity,
            "lockfile_sha256": self.lockfile_sha256,
            "runner_sha256": self.runner_sha256,
        }


SRT_RELEASE = SrtRelease(
    package="@anthropic-ai/sandbox-runtime",
    version="0.0.66",
    tarball_url=(
        "https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/sandbox-runtime-0.0.66.tgz"
    ),
    tarball_sha256="10088a88db2d734d3a7ccf57d83e0b781ab08669361b45947637e3fd51d7c4ee",
    npm_integrity=(
        "sha512-OE7QiGZJXe7ZshP47U2vk2z9FGSyiSN4ca9krVrE28LS2Qj0AHRWZz+"
        "gAce6FzG3gx/4OjNFwIhDuHXnI0WWwA=="
    ),
    lockfile_sha256="aa0e24fece2864c9a561db55ac5d528af202b17107675be89c1bce65c289ee3f",
    runner_sha256="389f2d49cabfc35100fb30939f37581edf2ab355a7586067a8c3de176dd890af",
)


@dataclass(frozen=True)
class ImpeccableRelease:
    """Immutable integrity contract for Gobby's managed Impeccable CLI."""

    package: str
    version: str
    lockfile_sha256: str

    def receipt_fields(self) -> dict[str, str]:
        return asdict(self)


IMPECCABLE_RELEASE = ImpeccableRelease(
    package="impeccable",
    version="3.5.0",
    lockfile_sha256="c6d77077e7001fbd4777bf564f92b6430a8df924424970a6eba959c571aa2fa3",
)
IMPECCABLE_NODE_MIN_VERSION = "22.18.0"


@dataclass(frozen=True)
class DependencyStatus:
    state: DependencyState
    installed_version: str | None
    minimum_version: str | None
    expected_version: str | None
    path: str | None
    error: str | None

    def to_payload(self) -> DependencyPayload:
        return asdict(self)


@dataclass(frozen=True)
class DetectedVersion:
    display: str
    numeric: tuple[int, ...]


@dataclass(frozen=True)
class DependencyReport:
    runtime: dict[str, DependencyStatus]
    required: dict[str, DependencyStatus]
    optional: dict[str, DependencyStatus]
    services: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "runtime": {name: status.to_payload() for name, status in self.runtime.items()},
            "dependencies": {
                "required": {name: status.to_payload() for name, status in self.required.items()},
                "optional": {name: status.to_payload() for name, status in self.optional.items()},
            },
            "services": self.services,
        }


def is_native_windows() -> bool:
    """Return whether this process is running on native Windows rather than WSL."""
    return os.name == "nt" or platform.system() == "Windows"


def unsupported_platform_error() -> str | None:
    if not is_native_windows():
        return None
    return (
        "Native Windows is unsupported. Install WSL 2 with `wsl --install`, "
        "then install and run Gobby inside WSL."
    )


def requires_tmux() -> bool:
    """Return whether the supported host uses tmux for terminal sessions."""
    return os.name == "posix" and not is_native_windows()


def parse_version_output(output: str) -> DetectedVersion | None:
    """Extract a comparable semantic version from common CLI version output."""
    match = _VERSION_PATTERN.search(output.strip())
    if match is None:
        return None
    numeric = tuple(int(part) for part in match.group("numeric").split("."))
    return DetectedVersion(
        display=f"{match.group('numeric')}{match.group('suffix')}",
        numeric=numeric,
    )


def evaluate_version(
    *,
    name: str,
    output: str | None,
    path: str | None,
    minimum_version: str | None = None,
    expected_version: str | None = None,
    install_action: str,
) -> DependencyStatus:
    """Classify a discovered dependency against a minimum or exact contract."""
    if path is None:
        requirement = _requirement_text(minimum_version, expected_version)
        return DependencyStatus(
            state="missing",
            installed_version=None,
            minimum_version=minimum_version,
            expected_version=expected_version,
            path=None,
            error=(
                f"{name} is missing; detected version unavailable, requires {requirement}. "
                f"{install_action}"
            ),
        )

    detected = parse_version_output(output or "")
    if detected is None:
        raw_version = output.strip() if output and output.strip() else None
        requirement = _requirement_text(minimum_version, expected_version)
        detected_text = repr(raw_version) if raw_version is not None else "unavailable"
        return DependencyStatus(
            state="invalid",
            installed_version=raw_version,
            minimum_version=minimum_version,
            expected_version=expected_version,
            path=path,
            error=(
                f"{name} version is invalid; detected {detected_text}, requires {requirement}. "
                f"{install_action}"
            ),
        )

    state: DependencyState = "healthy"
    if expected_version is not None:
        expected = parse_version_output(expected_version)
        if expected is None or detected.display != expected.display:
            state = "invalid"
    elif minimum_version is not None:
        minimum = parse_version_output(minimum_version)
        if minimum is None:
            state = "invalid"
        else:
            detected_numeric, minimum_numeric = _normalize_numeric(
                detected.numeric, minimum.numeric
            )
            if detected_numeric < minimum_numeric:
                state = "outdated"

    error = None
    if state != "healthy":
        requirement = _requirement_text(minimum_version, expected_version)
        error = (
            f"{name} is {state}; detected {detected.display}, requires {requirement}. "
            f"{install_action}"
        )
    return DependencyStatus(
        state=state,
        installed_version=detected.display,
        minimum_version=minimum_version,
        expected_version=expected_version,
        path=path,
        error=error,
    )


def collect_dependency_report(
    *,
    managed_services: bool,
    include_srt: bool,
) -> DependencyReport:
    """Collect every shared dependency contract and service runtime version."""
    runtime = {"python": _python_status()}
    required: dict[str, DependencyStatus] = {}
    if requires_tmux():
        required["tmux"] = _command_status(
            name="tmux",
            executable="tmux",
            arguments=("-V",),
            minimum_version=TMUX_MIN_VERSION,
            install_action="Install tmux 3.2 or newer and retry.",
        )
    required["git"] = _command_status(
        name="Git",
        executable="git",
        arguments=("--version",),
        minimum_version=GIT_MIN_VERSION,
        install_action="Install Git 2.38.0 or newer and retry.",
    )
    required["node"] = node_dependency_status()
    if include_srt:
        required["srt"] = srt_dependency_status()

    compose = _command_status(
        name="Docker Compose",
        executable="docker",
        arguments=("compose", "version", "--short"),
        minimum_version=DOCKER_COMPOSE_MIN_VERSION if managed_services else None,
        install_action="Install Docker Compose 2.7.0 or newer and retry.",
    )
    if managed_services:
        required["docker_compose"] = compose

    optional = {
        "tailscale": _command_status(
            name="Tailscale",
            executable="tailscale",
            arguments=("version",),
            install_action="Reinstall Tailscale to restore version reporting.",
        ),
        "impeccable": impeccable_dependency_status(),
    }
    docker = _command_status(
        name="Docker Engine",
        executable="docker",
        arguments=("--version",),
        install_action="Install Docker Engine and retry.",
    )
    services: dict[str, object] = {
        "docker": docker.to_payload(),
        "docker_running": _docker_running(docker.path),
        "docker_compose": compose.to_payload(),
        "managed_local_services": managed_services,
    }
    return DependencyReport(
        runtime=runtime,
        required=required,
        optional=optional,
        services=services,
    )


def required_dependency_errors(report: DependencyReport) -> list[str]:
    """Return actionable errors for runtime and required dependency failures."""
    statuses = [*report.runtime.values(), *report.required.values()]
    return [
        status.error or "Required dependency is unhealthy."
        for status in statuses
        if status.state != "healthy"
    ]


def node_dependency_status() -> DependencyStatus:
    """Return the shared Node.js runtime contract used by managed SRT."""
    return _command_status(
        name="Node.js",
        executable="node",
        arguments=("--version",),
        minimum_version=NODE_MIN_VERSION,
        install_action="Install Node.js 20.11.0 or newer and retry.",
    )


def _python_status() -> DependencyStatus:
    return evaluate_version(
        name="Python",
        output=platform.python_version(),
        path=str(Path(sys.executable).resolve(strict=False)),
        minimum_version=PYTHON_MIN_VERSION,
        install_action="Install Python 3.13.0 or newer and retry.",
    )


def _command_status(
    *,
    name: str,
    executable: str,
    arguments: tuple[str, ...],
    install_action: str,
    minimum_version: str | None = None,
) -> DependencyStatus:
    raw_path = shutil.which(executable)
    if raw_path is None:
        return evaluate_version(
            name=name,
            output=None,
            path=None,
            minimum_version=minimum_version,
            install_action=install_action,
        )
    try:
        path = str(Path(raw_path).resolve(strict=True))
        result = subprocess.run(  # Executable resolved from PATH. # nosec B603
            [path, *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        output: str | None = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            output = None
    except (OSError, RuntimeError, subprocess.SubprocessError):
        path = str(Path(raw_path).resolve(strict=False))
        output = None
    return evaluate_version(
        name=name,
        output=output,
        path=path,
        minimum_version=minimum_version,
        install_action=install_action,
    )


def srt_dependency_status() -> DependencyStatus:
    """Return exact-version and integrity status for Gobby's managed SRT."""
    from gobby.agents.srt_runtime import srt_install_root, verify_srt_installation

    root = srt_install_root()
    installed_version = _read_srt_package_version(root)
    try:
        installation = verify_srt_installation()
    except Exception as exc:
        exists = root.exists()
        state: DependencyState = "invalid" if exists else "missing"
        detected = installed_version if installed_version is not None else "unavailable"
        return DependencyStatus(
            state=state,
            installed_version=installed_version,
            minimum_version=None,
            expected_version=SRT_RELEASE.version,
            path=str(root.resolve(strict=False)) if exists else None,
            error=(
                f"SRT is {state}; detected {detected}, requires =={SRT_RELEASE.version}. "
                f"Run `gobby install` to repair the managed runtime ({exc})."
            ),
        )
    return DependencyStatus(
        state="healthy",
        installed_version=SRT_RELEASE.version,
        minimum_version=None,
        expected_version=SRT_RELEASE.version,
        path=str(installation.root),
        error=None,
    )


def impeccable_dependency_status() -> DependencyStatus:
    """Return exact-version and integrity status for managed Impeccable."""
    if is_native_windows():
        return DependencyStatus(
            state="healthy",
            installed_version="unsupported on native Windows",
            minimum_version=None,
            expected_version=IMPECCABLE_RELEASE.version,
            path=None,
            error=None,
        )
    from gobby.cli.install_setup_impeccable import (
        ImpeccableInstallError,
        inspect_impeccable_installation,
    )

    try:
        pointer = inspect_impeccable_installation()
    except ImpeccableInstallError as exc:
        return DependencyStatus(
            state="invalid",
            installed_version=None,
            minimum_version=None,
            expected_version=IMPECCABLE_RELEASE.version,
            path=None,
            error=(
                "Impeccable is invalid; "
                f"requires =={IMPECCABLE_RELEASE.version}. Run `gobby install` to repair ({exc})."
            ),
        )
    if pointer is None:
        return DependencyStatus(
            state="missing",
            installed_version=None,
            minimum_version=None,
            expected_version=IMPECCABLE_RELEASE.version,
            path=None,
            error=(
                "Impeccable is missing; "
                f"requires =={IMPECCABLE_RELEASE.version}. Run `gobby install` to repair."
            ),
        )
    return DependencyStatus(
        state="healthy",
        installed_version=IMPECCABLE_RELEASE.version,
        minimum_version=None,
        expected_version=IMPECCABLE_RELEASE.version,
        path=str(pointer),
        error=None,
    )


def _read_srt_package_version(root: Path) -> str | None:
    package_json = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
    try:
        value = json.loads(package_json.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _docker_running(path: str | None) -> bool:
    if path is None:
        return False
    try:
        result = subprocess.run(  # Absolute Docker executable. # nosec B603
            [path, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _normalize_numeric(
    left: tuple[int, ...], right: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _requirement_text(minimum_version: str | None, expected_version: str | None) -> str:
    if expected_version is not None:
        return f"=={expected_version}"
    if minimum_version is not None:
        return f">={minimum_version}"
    return "a verifiable version"
