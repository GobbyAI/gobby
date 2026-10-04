"""Focused tests for the run-local sandbox subprocess environment."""

from pathlib import Path

import pytest

from gobby.agents.sandbox_run_environment import SandboxRunPaths


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_environment_disables_ocr_update_check(provider: str, tmp_path: Path) -> None:
    run = SandboxRunPaths(
        root=tmp_path,
        assets=tmp_path / "assets",
        tmp=tmp_path / "tmp",
        hooks=tmp_path / "hooks",
        logs=tmp_path / "logs",
        cache=tmp_path / "cache",
        shared_cache=tmp_path / "shared",
        cargo_target=tmp_path / "shared" / "target",
    )

    assert run.environment(provider)["OCR_NO_UPDATE"] == "1"
