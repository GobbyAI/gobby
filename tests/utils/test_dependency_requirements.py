"""Tests for the shared runtime dependency contract."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from gobby.agents import srt_runtime
from gobby.agents.srt_runtime import SrtInstallation, SrtRuntimeError
from gobby.utils import dependency_requirements as requirements
from gobby.utils.dependency_requirements import (
    SRT_RELEASE,
    DependencyStatus,
    evaluate_version,
    parse_version_output,
    srt_dependency_status,
)

pytestmark = pytest.mark.unit


def _raise_srt_runtime_error(message: str) -> None:
    raise SrtRuntimeError(message)


def _collect_dependency_report(
    monkeypatch: pytest.MonkeyPatch,
    *,
    managed_services: bool,
) -> tuple[requirements.DependencyReport, DependencyStatus, list[str | None]]:
    healthy = DependencyStatus(
        state="healthy",
        installed_version="99.0.0",
        minimum_version=None,
        expected_version=None,
        path="/bin/tool",
        error=None,
    )
    compose_minimums: list[str | None] = []

    def command_status(**kwargs: object) -> DependencyStatus:
        if kwargs["arguments"] == ("compose", "version", "--short"):
            minimum = kwargs.get("minimum_version")
            compose_minimums.append(minimum if isinstance(minimum, str) else None)
        return healthy

    monkeypatch.setattr(requirements, "_python_status", lambda: healthy)
    monkeypatch.setattr(requirements, "_command_status", command_status)
    monkeypatch.setattr(requirements, "node_dependency_status", lambda: healthy)
    monkeypatch.setattr(requirements, "impeccable_dependency_status", lambda: healthy)
    monkeypatch.setattr(requirements, "_docker_running", lambda _path: True)

    report = requirements.collect_dependency_report(
        managed_services=managed_services,
        include_srt=False,
    )
    return report, healthy, compose_minimums


@pytest.mark.parametrize(
    ("minimum", "exact", "below", "newer"),
    [
        ("3.13.0", "3.13.0", "3.12.9", "3.14.0"),
        ("3.2", "tmux 3.2", "tmux 3.1", "tmux 3.7b"),
        ("2.38.0", "git version 2.38.0", "git version 2.37.9", "git version 2.50.1"),
        ("20.11.0", "v20.11.0", "v20.10.9", "v26.5.0"),
        ("2.7.0", "Docker Compose version v2.7.0", "v2.6.9", "v2.39.1"),
    ],
)
def test_minimum_version_boundaries(
    minimum: str,
    exact: str,
    below: str,
    newer: str,
) -> None:
    def classify(output: str) -> str:
        return evaluate_version(
            name="dependency",
            output=output,
            path="/usr/bin/dependency",
            minimum_version=minimum,
            install_action="Install dependency.",
        ).state

    assert classify(exact) == "healthy"
    assert classify(below) == "outdated"
    assert classify(newer) == "healthy"


@pytest.mark.parametrize("output", ["", "unknown", "version twenty", "v20"])
def test_malformed_required_versions_fail_closed(output: str) -> None:
    status = evaluate_version(
        name="Node.js",
        output=output,
        path="/usr/bin/node",
        minimum_version="20.11.0",
        install_action="Install Node.js.",
    )

    assert status.state == "invalid"
    assert "requires >=20.11.0" in (status.error or "")
    assert "Install Node.js" in (status.error or "")


@pytest.mark.parametrize(
    ("output", "display"),
    [
        ("tmux 3.7b", "3.7b"),
        ("v26.5.0", "26.5.0"),
        ("git version 2.50.1 (Apple Git-155)", "2.50.1"),
    ],
)
def test_common_cli_version_formats(output: str, display: str) -> None:
    parsed = parse_version_output(output)

    assert parsed is not None
    assert parsed.display == display


def test_missing_dependency_has_actionable_contract() -> None:
    status = evaluate_version(
        name="Git",
        output=None,
        path=None,
        minimum_version="2.38.0",
        install_action="Install Git 2.38.0 or newer and retry.",
    )

    assert status.state == "missing"
    assert status.installed_version is None
    assert status.minimum_version == "2.38.0"
    assert status.path is None
    assert "detected version unavailable" in (status.error or "")


def test_exact_version_contract_rejects_wrong_srt_version() -> None:
    status = evaluate_version(
        name="SRT",
        output="0.0.65",
        path="/tools/srt",
        expected_version=SRT_RELEASE.version,
        install_action="Run `gobby install`.",
    )

    assert status.state == "invalid"
    assert status.expected_version == SRT_RELEASE.version


def test_srt_status_healthy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "srt"
    installation = SrtInstallation(
        root=root,
        node=tmp_path / "node",
        runner=root / "runner.mjs",
        package_json=root / "package.json",
    )
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(srt_runtime, "verify_srt_installation", lambda: installation)

    status = srt_dependency_status()

    assert status.state == "healthy"
    assert status.installed_version == SRT_RELEASE.version
    assert status.expected_version == SRT_RELEASE.version


def test_srt_status_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "missing"
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(
        srt_runtime,
        "verify_srt_installation",
        partial(_raise_srt_runtime_error, "missing"),
    )

    status = srt_dependency_status()

    assert status.state == "missing"
    assert status.path is None
    assert "gobby install" in (status.error or "")


@pytest.mark.parametrize(
    ("installation", "error", "expected_state"),
    [
        (None, None, "missing"),
        (Path("/managed/impeccable"), None, "healthy"),
        (None, "corrupt", "invalid"),
    ],
)
def test_impeccable_dependency_status(
    monkeypatch: pytest.MonkeyPatch,
    installation: Path | None,
    error: str | None,
    expected_state: str,
) -> None:
    from gobby.cli import install_setup_impeccable as impeccable

    monkeypatch.setattr(requirements, "is_native_windows", lambda: False)
    if error is None:
        monkeypatch.setattr(
            impeccable,
            "inspect_impeccable_installation",
            lambda: installation,
        )
    else:
        monkeypatch.setattr(
            impeccable,
            "inspect_impeccable_installation",
            lambda: (_ for _ in ()).throw(impeccable.ImpeccableInstallError(error)),
        )

    status = requirements.impeccable_dependency_status()

    assert status.state == expected_state
    if expected_state != "healthy":
        assert status.error is not None
        assert "gobby install" in status.error


def test_impeccable_dependency_status_reports_native_windows_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requirements, "is_native_windows", lambda: True)

    status = requirements.impeccable_dependency_status()

    assert status.state == "healthy"
    assert status.installed_version == "unsupported on native Windows"
    assert status.error is None


@pytest.mark.parametrize(
    ("package_version", "verification_error"),
    [
        (None, "runner checksum mismatch"),
        (SRT_RELEASE.version, "receipt does not match"),
        ("0.0.65", "package identity mismatch"),
    ],
)
def test_srt_status_invalid_installations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    package_version: str | None,
    verification_error: str,
) -> None:
    root = tmp_path / "srt"
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True)
    if package_version is not None:
        (package_dir / "package.json").write_text(
            json.dumps({"version": package_version}),
            encoding="utf-8",
        )
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(
        srt_runtime,
        "verify_srt_installation",
        partial(_raise_srt_runtime_error, verification_error),
    )

    status = srt_dependency_status()

    assert status.state == "invalid"
    assert status.installed_version == package_version
    assert verification_error in (status.error or "")


def test_external_services_skip_compose_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    report, healthy, compose_minimums = _collect_dependency_report(
        monkeypatch,
        managed_services=False,
    )

    assert "docker_compose" not in report.required
    assert report.services["docker_compose"] == healthy.to_payload()
    assert compose_minimums == [None]


def test_managed_services_require_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    report, healthy, compose_minimums = _collect_dependency_report(
        monkeypatch,
        managed_services=True,
    )

    assert report.runtime == {"python": healthy}
    assert report.required["git"] == healthy
    assert report.required["node"] == healthy
    assert report.required["docker_compose"] == healthy
    assert compose_minimums == ["2.7.0"]
    assert report.optional == {"tailscale": healthy, "impeccable": healthy}
    assert report.services["docker_running"] is True


@pytest.mark.parametrize(
    ("os_name", "platform_name", "error_fragment", "requires_tmux"),
    [
        ("nt", "Windows", "WSL 2", False),
        ("posix", "Linux", None, True),
    ],
)
def test_platform_support_and_tmux_requirement(
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
    platform_name: str,
    error_fragment: str | None,
    requires_tmux: bool,
) -> None:
    monkeypatch.setattr("gobby.utils.dependency_requirements.os.name", os_name)
    monkeypatch.setattr(
        "gobby.utils.dependency_requirements.platform.system",
        lambda: platform_name,
    )
    error = requirements.unsupported_platform_error()
    if error_fragment is None:
        assert error is None
    else:
        assert error_fragment in (error or "")
    assert requirements.requires_tmux() is requires_tmux
