"""Schema-focused checks for ``ghook --diagnose`` output."""

from __future__ import annotations

import pytest

from .runner import ALL_SANDBOX_SPECS, SandboxRunner, validate_diagnose_payload

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize(
    ("cli_name", "hook_type"),
    [(spec.cli_name, spec.hook_type) for spec in ALL_SANDBOX_SPECS],
)
def test_live_diagnose_output_validates_against_schema(cli_name: str, hook_type: str) -> None:
    spec = next(
        spec
        for spec in ALL_SANDBOX_SPECS
        if spec.cli_name == cli_name and spec.hook_type == hook_type
    )
    runner = SandboxRunner(spec)
    result = runner.run_diagnose()

    validate_diagnose_payload(result.payload)
