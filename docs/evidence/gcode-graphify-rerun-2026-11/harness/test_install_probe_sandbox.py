"""Third-party Graphify probes in record_installations run only under SRT."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import record_installations
from runtime_boundary import EvidenceError


def _wrap(argv: list[str]) -> list[str]:
    return ["srt-wrapper", "--policy", "p.json", "--", *argv]


def _runtime(tmp_path: Path) -> Path:
    for relative in ("logs/install", "receipts"):
        (tmp_path / relative).mkdir(parents=True)
    return tmp_path


def test_probe_runs_the_wrapped_command_and_records_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime(tmp_path)
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    argv = [str(root / "tools/graphify/venv/bin/graphify"), "--version"]
    check = record_installations.probe(root, "a1", "graphify-version", argv, root, wrap=_wrap)
    assert ran == [_wrap(argv)]
    assert check["argv"] == argv
    assert check["sandboxed"] is True


def test_reused_receipt_without_the_sandbox_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
    )
    argv = [str(root / "tools/graphify/venv/bin/graphify"), "--version"]
    record_installations.probe(root, "a1", "graphify-version", argv, root)
    with pytest.raises(EvidenceError, match="without the required sandbox"):
        record_installations.probe(root, "a1", "graphify-version", argv, root, wrap=_wrap)


def test_only_the_graphify_version_probe_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    def fake_probe(
        root: Path,
        attempt: str,
        label: str,
        argv: list[str],
        cwd: Path,
        env: Any = None,
        wrap: Any = None,
    ) -> dict[str, Any]:
        calls[label] = (wrap, env)
        return {"argv": argv, "cwd": str(cwd), "exit_code": 0, "log": {"label": label}}

    sandbox_env = {"TMPDIR": str(tmp_path / "tmp/install-probe-rerun1")}
    monkeypatch.setattr(record_installations, "DEFAULT_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(record_installations, "assert_owned_runtime", lambda *_args: None)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        record_installations, "third_party_sandbox", lambda *_args: (_wrap, sandbox_env)
    )
    monkeypatch.setattr(record_installations, "probe", fake_probe)
    monkeypatch.setattr(record_installations, "_pypi_version", lambda *_args: "0.9.103")
    monkeypatch.setattr(record_installations, "read_archived_gcode_version", lambda _r: "1.0.0")
    monkeypatch.setattr(
        record_installations, "file_record", lambda _root, path: {"path": str(path)}
    )
    monkeypatch.setattr(record_installations, "validate_graphify_receipt", lambda *_args: None)
    monkeypatch.setattr(record_installations, "validate_gcode_receipt", lambda *_args: None)
    monkeypatch.setattr(record_installations, "write_once", lambda *_args: None)
    monkeypatch.setattr(
        sys, "argv", ["record_installations", "--owner-session", "owner", "--attempt", "rerun1"]
    )
    assert record_installations.main() == 0
    assert calls["graphify-version"] == (_wrap, sandbox_env)
    unwrapped = {"graphify-dependencies", "graphify-freeze", "gcode-version", "gcode-contract"}
    assert {label for label, (wrap, _env) in calls.items() if wrap is None} == unwrapped


def test_missing_sandbox_refuses_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []

    def unavailable(_root: Path, _attempt: str) -> Any:
        raise EvidenceError("SRT is not installed")

    monkeypatch.setattr(record_installations, "DEFAULT_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(record_installations, "assert_owned_runtime", lambda *_args: None)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(record_installations, "resolve_srt_runner", unavailable)
    monkeypatch.setattr(
        record_installations,
        "probe",
        lambda _root, _attempt, label, *_a, **_k: probed.append(label),
    )
    monkeypatch.setattr(
        sys, "argv", ["record_installations", "--owner-session", "owner", "--attempt", "rerun1"]
    )
    with pytest.raises(EvidenceError, match="SRT is not installed"):
        record_installations.main()
    assert probed == []


def test_probe_policy_denies_network_and_writes_outside_scratch(tmp_path: Path) -> None:
    interpreter_base = tmp_path / "uv-python"
    (interpreter_base / "bin").mkdir(parents=True)
    (interpreter_base / "bin/python3").touch()
    venv_bin = tmp_path / "root/tools/graphify/venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(interpreter_base / "bin/python3")
    scratch = tmp_path / "root/tmp/install-probe-a1"
    policy = record_installations.third_party_probe_policy(
        tmp_path / "root", tmp_path / "home", scratch
    )
    assert policy["network"]["allowedDomains"] == []
    assert policy["network"]["strictAllowlist"] is True
    assert policy["network"]["allowLocalBinding"] is False
    assert policy["network"]["allowAllUnixSockets"] is False
    assert policy["filesystem"]["allowWrite"] == [str(scratch)]
    assert str(tmp_path / "root") in policy["filesystem"]["allowRead"]
