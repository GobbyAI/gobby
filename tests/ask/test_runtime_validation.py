from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.ask.runtime_validation import (
    ASK_NATIVE_PROBE_EXPECTATIONS,
    AskRuntimeValidationArtifact,
    ask_runtime_control_digest,
    build_ask_runtime_probe_artifact,
    load_ask_runtime_validation,
    write_ask_runtime_probe_artifact,
)

pytestmark = pytest.mark.unit


def _provider(path: Path, version: str) -> Path:
    path.write_text(f"#!/bin/sh\necho 'Claude Code {version}'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _observations() -> list[dict[str, str]]:
    return [
        {
            "phase": phase,
            "case": case,
            "observed": expected,
            "receipt": f"{phase}:{case}:{expected}",
        }
        for phase in ("fresh", "resumed")
        for case, expected in ASK_NATIVE_PROBE_EXPECTATIONS.items()
    ]


def test_probe_artifact_binds_live_provider_version_and_control_digest(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    artifact = build_ask_runtime_probe_artifact(
        provider="claude",
        provider_executable=executable,
        auth_mode="claude.ai",
        control_digest=control_digest,
        observations=_observations(),
    )
    artifact_path = tmp_path / "probe.json"
    artifact_sha256 = write_ask_runtime_probe_artifact(artifact_path, artifact)
    reference = AskRuntimeValidationArtifact(
        path=artifact_path,
        sha256=artifact_sha256,
    )

    validation = load_ask_runtime_validation(
        reference,
        provider_executable=executable,
    )
    assert validation.provider_version == "2.1.265"
    assert validation.control_digest == control_digest
    assert validation.fresh_probe_passed is True
    assert validation.resume_probe_passed is True

    _provider(executable, "2.1.266")
    with pytest.raises(ValueError, match="executable|version"):
        load_ask_runtime_validation(reference, provider_executable=executable)

    artifact["control_digest"] = "0" * 64
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash"):
        load_ask_runtime_validation(reference, provider_executable=executable)


def test_probe_artifact_requires_every_fresh_and_resumed_boundary(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations()
    observations.pop()
    with pytest.raises(ValueError, match="matrix"):
        build_ask_runtime_probe_artifact(
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )
