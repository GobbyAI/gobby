"""Cross-language runtime configuration contract generation tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPOSITORY_ROOT / "scripts" / "generate_runtime_config_contract.py"
CONTRACT = (
    REPOSITORY_ROOT / "crates" / "gcore" / "assets" / "config" / "runtime_config_contract.json"
)


def test_checked_in_contract_matches_registry() -> None:
    """The checked-in Rust asset must equal fresh registry output byte-for-byte."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--stdout"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == CONTRACT.read_bytes()
