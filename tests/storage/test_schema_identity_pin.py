"""Contracts for the shared gdaemon schema identity probe and pin formats."""

from __future__ import annotations

import subprocess
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.storage import schema_contract, schema_identity_pin
from gobby.storage.schema_identity_pin import (
    SchemaIdentityError,
    pin_bytes,
    probe_identity,
    stamp_bytes,
    validate_identity,
)

IDENTITY: dict[str, int | str] = {
    "assets_root_hash": "assets",
    "baseline_checksum": "baseline",
    "baseline_version": 1,
    "latest_checksum": "latest",
    "latest_version": 2,
    "runner_protocol": 1,
}


def test_pin_bytes_reproduces_the_packaged_pin_exactly() -> None:
    packaged = resources.files("gobby.storage").joinpath("schema_expected_identity.json")

    assert pin_bytes(schema_contract.expected_schema_identity()) == packaged.read_bytes()


def test_stamp_bytes_matches_the_installer_identity_stamp() -> None:
    expected = f"{schema_contract.expected_schema_identity_json()}\n".encode()

    assert stamp_bytes(schema_contract.expected_schema_identity()) == expected


def test_validate_identity_returns_the_exact_contract() -> None:
    assert validate_identity(dict(IDENTITY)) == IDENTITY


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (["not", "an", "object"], "must be a JSON object"),
        ({**IDENTITY, "extra": 1}, "must contain exactly"),
        ({key: value for key, value in IDENTITY.items() if key != "assets_root_hash"}, "exactly"),
        ({**IDENTITY, "runner_protocol": True}, "field runner_protocol must be an integer"),
        ({**IDENTITY, "latest_version": "2"}, "field latest_version must be an integer"),
        ({**IDENTITY, "latest_checksum": ""}, "field latest_checksum must be a string"),
        ({**IDENTITY, "baseline_checksum": 7}, "field baseline_checksum must be a string"),
    ],
)
def test_validate_identity_rejects_contract_violations(payload: object, message: str) -> None:
    with pytest.raises(SchemaIdentityError, match=message):
        validate_identity(payload)


def _stub_subprocess(monkeypatch: pytest.MonkeyPatch, run: object) -> None:
    monkeypatch.setattr(
        schema_identity_pin,
        "subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )


def test_probe_identity_runs_schema_version_and_validates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            '{"assets_root_hash": "assets", '
            '"baseline_checksum": "baseline", "baseline_version": 1, '
            '"latest_checksum": "latest", "latest_version": 2, "runner_protocol": 1}',
            "",
        )

    _stub_subprocess(monkeypatch, run)
    gdaemon = tmp_path / "gdaemon"

    assert probe_identity(gdaemon, cwd=tmp_path) == IDENTITY
    assert calls[0][0] == [str(gdaemon), "schema", "version", "--json"]
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["timeout"] == 30


@pytest.mark.parametrize(
    ("behaviour", "message"),
    [
        ("timeout", "gdaemon schema version timed out after 30 seconds"),
        ("oserror", "failed to launch gdaemon: no such binary"),
        ("nonzero", "gdaemon schema version failed: boom"),
        ("invalid-json", "gdaemon schema version returned invalid JSON"),
    ],
)
def test_probe_identity_reports_each_failure_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, behaviour: str, message: str
) -> None:
    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        if behaviour == "oserror":
            raise OSError("no such binary")
        if behaviour == "nonzero":
            return subprocess.CompletedProcess(args, 1, "", "boom\n")
        return subprocess.CompletedProcess(args, 0, "not json", "")

    _stub_subprocess(monkeypatch, run)

    with pytest.raises(SchemaIdentityError, match=message):
        probe_identity(tmp_path / "gdaemon")
