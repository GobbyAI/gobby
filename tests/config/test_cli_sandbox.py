"""Tests for per-CLI sandbox daemon config."""

from __future__ import annotations

import pytest

from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


class TestCLISandboxConfig:
    def test_daemon_config_includes_per_cli_sandbox_defaults(self) -> None:
        config = DaemonConfig()

        assert config.cli_sandbox.claude.enabled is True
        assert config.cli_sandbox.codex.enabled is True
        assert config.cli_sandbox.gemini.enabled is True
        assert config.cli_sandbox.qwen.enabled is True
        assert config.cli_sandbox.codex.mode == "permissive"
        assert config.cli_sandbox.gemini.allow_network is True

    def test_daemon_config_accepts_nested_provider_overrides(self) -> None:
        config = DaemonConfig(
            cli_sandbox={
                "codex": {
                    "enabled": True,
                    "mode": "restrictive",
                    "allow_network": False,
                    "extra_write_paths": ["/tmp/codex-cache"],
                },
                "claude": {
                    "enabled": False,
                },
            }
        )

        assert config.cli_sandbox.codex.mode == "restrictive"
        assert config.cli_sandbox.codex.allow_network is False
        assert config.cli_sandbox.codex.extra_write_paths == ["/tmp/codex-cache"]
        assert config.cli_sandbox.claude.enabled is False
        assert config.cli_sandbox.gemini.enabled is True
