"""Phase 7 cleanup audits for legacy stage-skip label readers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]

RUNTIME_SCOPE = (
    "src/gobby/dispatch/",
    "src/gobby/build/",
    "src/gobby/cli/",
    "src/gobby/mcp_proxy/",
    "src/gobby/servers/",
    "src/gobby/tasks/expansion/",
    "src/gobby/tasks/expansion_service.py",
    "src/gobby/storage/tasks/",
)
INSTRUCTION_SCOPE = (
    "src/gobby/install/shared/workflows/agents/",
    "src/gobby/install/shared/skills/",
)
ACTIVE_GUIDE_SCOPE = (
    "CLAUDE.md",
    "docs/guides/",
)


def _git_grep(pattern: str, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "grep", "-nE", pattern, "--", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_no_matches(result: subprocess.CompletedProcess[str]) -> None:
    assert result.stderr == ""
    assert result.stdout == ""


def test_grep_returns_empty_for_full_runtime_scope() -> None:
    result = _git_grep(r"_SKIP_PREFIX|_skipped_stages|stage-:", *RUNTIME_SCOPE)

    _assert_no_matches(result)


def test_grep_returns_empty_for_bundled_agent_instructions() -> None:
    result = _git_grep(r"_skipped_stages|stage-:", *INSTRUCTION_SCOPE)

    _assert_no_matches(result)


def test_active_guides_do_not_describe_legacy_stage_skip_labels() -> None:
    result = _git_grep(r"stage-:", *ACTIVE_GUIDE_SCOPE)

    _assert_no_matches(result)


def test_expansion_service_facade_does_not_export_skipped_stages() -> None:
    from gobby.tasks import expansion_service

    assert not hasattr(expansion_service, "_skipped_stages")
    assert "_skipped_stages" not in getattr(expansion_service, "__all__", ())


def test_migration_runner_no_longer_carries_historical_stage_skip_helpers() -> None:
    source = (ROOT / "src/gobby/storage/migrations.py").read_text(encoding="utf-8")

    assert "def _stage_skip_labels" not in source
    assert "stage-:" not in source
    assert "def _backfill_task_stage_states_from_legacy" not in source
