"""Tests for the packaged gdaemon schema identity generator."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import generate_schema_expected_identity as generator


def test_generate_writes_stable_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = {
        "runner_protocol": 1,
        "baseline_version": 375,
        "baseline_checksum": "baseline",
        "latest_version": 375,
        "latest_checksum": "latest",
        "assets_root_hash": "root",
    }

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = ["/managed/gdaemon", "schema", "version", "--json"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(identity), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    output = tmp_path / "identity.json"

    generator.generate(Path("/managed/gdaemon"), output)

    assert json.loads(output.read_text(encoding="utf-8")) == identity
    assert (
        output.read_text(encoding="utf-8")
        == json.dumps(
            identity,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_generate_rejects_incomplete_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["/managed/gdaemon", "schema", "version", "--json"],
            0,
            stdout='{"runner_protocol": 1}',
            stderr="",
        ),
    )

    with pytest.raises(generator.IdentityGenerationError, match="exactly"):
        generator.generate(Path("/managed/gdaemon"), tmp_path / "identity.json")
