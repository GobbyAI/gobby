"""Tests for HookExtensionsConfig."""

from gobby.config.app import DaemonConfig, HookExtensionsConfig, WebSocketBroadcastConfig


def test_hook_extensions_defaults():
    """Test default values for hook extensions."""
    config = DaemonConfig()

    assert isinstance(config.hook_extensions, HookExtensionsConfig)
    assert isinstance(config.hook_extensions.websocket, WebSocketBroadcastConfig)

    # Check websocket defaults
    assert config.hook_extensions.websocket.enabled is True
    assert config.hook_extensions.websocket.include_payload is True
    assert "session-start" in config.hook_extensions.websocket.broadcast_events
    assert "pre-tool-use" in config.hook_extensions.websocket.broadcast_events


def test_hook_extensions_custom_values():
    """Test setting custom values for hook extensions."""
    config_dict = {
        "hook_extensions": {
            "websocket": {
                "enabled": False,
                "broadcast_events": ["notification"],
                "include_payload": False,
            }
        }
    }

    config = DaemonConfig(**config_dict)

    assert config.hook_extensions.websocket.enabled is False
    assert config.hook_extensions.websocket.include_payload is False
    assert config.hook_extensions.websocket.broadcast_events == ["notification"]
