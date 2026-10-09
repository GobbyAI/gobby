"""Cross-language runtime configuration contract generation tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPOSITORY_ROOT / "scripts" / "generate_runtime_config_contract.py"
CONTRACT = (
    REPOSITORY_ROOT / "crates" / "gcore" / "assets" / "config" / "runtime_config_contract.json"
)
WEB_VECTORS = REPOSITORY_ROOT / "web" / "src" / "api" / "runtimeConfigCodecVectors.gen.ts"


def _generated(flag: str) -> bytes:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), flag],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def test_checked_in_contract_matches_registry() -> None:
    """The checked-in Rust asset must equal fresh registry output byte-for-byte."""
    assert _generated("--stdout") == CONTRACT.read_bytes()


def test_spawn_cap_default_and_minimum() -> None:
    assert DaemonConfig().max_active_agents == 20
    assert DaemonConfig(max_active_agents=1).max_active_agents == 1
    with pytest.raises(ValidationError, match="max_active_agents"):
        DaemonConfig(max_active_agents=0)


def test_checked_in_web_codec_vectors_match_registry() -> None:
    """The generated web codec-vector fixture must equal fresh registry output."""
    assert _generated("--stdout-web") == WEB_VECTORS.read_bytes()
