from __future__ import annotations

import pytest

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(postgres_db: HubDatabase) -> HubDatabase:
    return postgres_db


@pytest.fixture
def config_store(temp_db: HubDatabase) -> ConfigStore:
    return ConfigStore(temp_db)


@pytest.fixture
def config_state() -> dict[str, DaemonConfig]:
    return {"config": DaemonConfig()}


@pytest.fixture
def config_registry(
    config_store: ConfigStore,
    config_state: dict[str, DaemonConfig],
) -> InternalToolRegistry:
    return create_config_registry(
        config=config_state["config"],
        config_store=config_store,
        config_setter=lambda config: config_state.__setitem__("config", config),
    )


def test_mcp_config_gets_indexing_default(config_registry: InternalToolRegistry) -> None:
    result = config_registry.get_tool("get_config")(key="indexing.respect_gitignore")

    assert result["success"] is True
    assert result["value"] is True


def test_mcp_config_sets_indexing_respect_gitignore(
    config_registry: InternalToolRegistry,
    config_store: ConfigStore,
    config_state: dict[str, DaemonConfig],
) -> None:
    result = config_registry.get_tool("set_config")(
        key="indexing.respect_gitignore",
        value=False,
    )

    assert result["success"] is True
    assert result["value"] is False
    assert config_store.get("indexing.respect_gitignore") is False
    assert config_state["config"].indexing.respect_gitignore is False
