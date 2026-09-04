"""Focused tests for provider-specific spawn configuration."""

from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides


def test_codex_shell_tmpdir_points_at_sandbox_scratchpad() -> None:
    scratchpad = "/Users/dev/.gobby/run/sandbox/run-1/tmp"

    overrides = _codex_mcp_config_overrides("/repo", scratchpad)

    assert f'shell_environment_policy.set.TMPDIR="{scratchpad}"' in overrides


def test_codex_shell_tmpdir_is_omitted_without_sandbox() -> None:
    overrides = _codex_mcp_config_overrides("/repo", None)

    assert not any(entry.startswith("shell_environment_policy.set.TMPDIR") for entry in overrides)
