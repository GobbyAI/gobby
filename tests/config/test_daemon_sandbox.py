"""Tests for daemon-owned sandbox configuration defaults."""

from __future__ import annotations

import pytest

from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


class TestDaemonSandboxConfig:
    def test_daemon_config_includes_daemon_owned_sandbox_defaults(self) -> None:
        config = DaemonConfig()

        assert config.web_chat_sandbox.enabled is True
        assert config.web_chat_sandbox.backend == "srt"
        assert config.web_chat_sandbox.mode == "permissive"
        assert config.web_chat_sandbox.allow_network is False
        assert config.web_chat_sandbox.extra_read_paths == []
        assert config.web_chat_sandbox.extra_write_paths == []
        assert config.agent_sandbox.enabled is True
        assert config.agent_sandbox.backend == "srt"
        assert config.agent_sandbox.mode == "permissive"
        assert config.agent_sandbox.allow_network is False
        assert config.agent_sandbox.extra_read_paths == []
        assert config.agent_sandbox.extra_write_paths == []
        assert not hasattr(config, "cli_sandbox")

    def test_supports_explicit_web_chat_and_agent_overrides(self) -> None:
        config = DaemonConfig(
            web_chat_sandbox={
                "enabled": False,
                "backend": "srt",
                "mode": "restrictive",
                "allow_network": False,
                "extra_write_paths": ["/tmp/web-chat-cache"],
            },
            agent_sandbox={
                "enabled": False,
                "backend": "srt",
                "mode": "restrictive",
                "allow_network": False,
                "extra_read_paths": ["/tmp/agent-shared"],
            },
        )

        assert config.web_chat_sandbox.enabled is False
        assert config.web_chat_sandbox.backend == "srt"
        assert config.web_chat_sandbox.mode == "restrictive"
        assert config.web_chat_sandbox.allow_network is False
        assert config.web_chat_sandbox.extra_write_paths == ["/tmp/web-chat-cache"]
        assert config.agent_sandbox.enabled is False
        assert config.agent_sandbox.backend == "srt"
        assert config.agent_sandbox.mode == "restrictive"
        assert config.agent_sandbox.allow_network is False
        assert config.agent_sandbox.extra_read_paths == ["/tmp/agent-shared"]

    @pytest.mark.parametrize("field", ["web_chat_sandbox", "agent_sandbox"])
    def test_rejects_provider_native_backend(self, field: str) -> None:
        with pytest.raises(ValueError, match="Input should be 'srt'"):
            DaemonConfig.model_validate({field: {"backend": "provider-native"}})

    def test_rejects_invalid_sandbox_mode(self) -> None:
        with pytest.raises(ValueError, match="Input should be 'permissive' or 'restrictive'"):
            DaemonConfig(web_chat_sandbox={"mode": "unrestricted"})
