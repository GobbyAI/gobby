"""Drift and default checks for the versioned gcore tool-loop contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby._generated_tool_loop_limits import (
    DEFAULT_LOOP_TIMEOUT_SECONDS,
    DEFAULT_MAX_BYTES_PER_TOOL_RESULT,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    TOOL_LOOP_CONFIG_PREFIX,
    TOOL_LOOP_CONTRACT,
    TOOL_LOOP_CONTRACT_VERSION,
)
from gobby.config.ai import ToolLoopConfig

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "crates/gcore/contracts/tool_loop_limits.v1.json"


def test_generated_python_binding_matches_gcore_contract() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate_tool_loop_limits.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_contract_metadata_and_defaults_are_canonical() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert TOOL_LOOP_CONTRACT == contract["contract"] == "gobby.tool_loop_limits"
    assert TOOL_LOOP_CONTRACT_VERSION == contract["version"] == 1
    assert TOOL_LOOP_CONFIG_PREFIX == contract["config_prefix"] == "ai.generation.tool_loop"
    assert DEFAULT_MAX_TURNS is contract["fields"]["max_turns"]["default"] is None
    assert DEFAULT_MAX_TOOL_CALLS == contract["fields"]["max_tool_calls"]["default"] == 24
    assert (
        DEFAULT_MAX_BYTES_PER_TOOL_RESULT
        == contract["fields"]["max_bytes_per_tool_result"]["default"]
        == 16_384
    )
    assert (
        DEFAULT_TOOL_TIMEOUT_SECONDS == contract["fields"]["tool_timeout_seconds"]["default"] == 300
    )
    assert (
        DEFAULT_LOOP_TIMEOUT_SECONDS
        == contract["fields"]["loop_timeout_seconds"]["default"]
        == 1_200
    )


def test_python_config_uses_generated_contract_defaults() -> None:
    config = ToolLoopConfig()

    assert config.model_dump() == {
        "max_turns": DEFAULT_MAX_TURNS,
        "max_tool_calls": DEFAULT_MAX_TOOL_CALLS,
        "max_bytes_per_tool_result": DEFAULT_MAX_BYTES_PER_TOOL_RESULT,
        "tool_timeout_seconds": DEFAULT_TOOL_TIMEOUT_SECONDS,
        "loop_timeout_seconds": DEFAULT_LOOP_TIMEOUT_SECONDS,
    }


@pytest.mark.parametrize(
    "field",
    [
        "max_turns",
        "max_tool_calls",
        "max_bytes_per_tool_result",
        "tool_timeout_seconds",
        "loop_timeout_seconds",
    ],
)
def test_python_config_rejects_zero_non_null_limits(field: str) -> None:
    with pytest.raises(ValidationError):
        ToolLoopConfig.model_validate({field: 0})
