"""
Tests for config.py MCP tools module.

Tests the config tools that provide read/write access to daemon configuration.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.config_store import ConfigStore, config_key_to_secret_name, is_secret_key_name
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(tmp_path: Path) -> LocalDatabase:
    """Create a temporary database with schema."""
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    return db


@pytest.fixture
def config_store(temp_db: LocalDatabase) -> ConfigStore:
    """Create a ConfigStore backed by temp database."""
    return ConfigStore(temp_db)


@pytest.fixture
def config_state() -> dict[str, DaemonConfig]:
    """Mutable state holder for tracking config updates."""
    return {"config": DaemonConfig()}


@pytest.fixture
def config_registry(config_store: ConfigStore, config_state: dict[str, DaemonConfig]):
    """Create a config registry with test fixtures."""
    return create_config_registry(
        config=config_state["config"],
        config_store=config_store,
        config_setter=lambda c: config_state.__setitem__("config", c),
    )


class TestGetConfig:
    """Tests for get_config tool."""

    def test_get_config_returns_value(self, config_registry) -> None:
        """Test get_config returns a known config value."""
        tool = config_registry.get_tool("get_config")
        result = tool(key="daemon_port")

        assert result["success"] is True
        assert result["key"] == "daemon_port"
        assert result["value"] == 60887

    def test_get_config_nested_value(self, config_registry) -> None:
        """Test get_config returns nested config values."""
        tool = config_registry.get_tool("get_config")
        result = tool(key="logging.level")

        assert result["success"] is True
        assert result["key"] == "logging.level"

    def test_get_config_missing_key(self, config_registry) -> None:
        """Test get_config returns error for missing key."""
        tool = config_registry.get_tool("get_config")
        result = tool(key="nonexistent.key.path")

        assert result["success"] is False
        assert "not found" in result["error"]


class TestGetConfigSection:
    """Tests for get_config_section tool."""

    def test_get_config_section_returns_nested_dict(self, config_registry) -> None:
        """Test get_config_section returns filtered nested dict."""
        tool = config_registry.get_tool("get_config_section")
        result = tool(prefix="logging")

        assert result["success"] is True
        assert result["prefix"] == "logging"
        assert isinstance(result["section"], dict)
        assert "level" in result["section"]

    def test_get_config_section_missing_prefix(self, config_registry) -> None:
        """Test get_config_section returns error for nonexistent prefix."""
        tool = config_registry.get_tool("get_config_section")
        result = tool(prefix="nonexistent_section")

        assert result["success"] is False
        assert "No keys found" in result["error"]

    def test_get_config_section_returns_subsection(self, config_registry) -> None:
        """Test get_config_section works with deeper prefixes."""
        tool = config_registry.get_tool("get_config_section")
        result = tool(prefix="logging")

        assert result["success"] is True
        section = result["section"]
        assert "level" in section


class TestSetConfig:
    """Tests for set_config tool."""

    def test_set_config_persists_to_db(self, config_registry, config_store: ConfigStore) -> None:
        """Test set_config persists the value to the database."""
        tool = config_registry.get_tool("set_config")
        result = tool(key="daemon_port", value=61000)

        assert result["success"] is True
        assert result["value"] == 61000

        # Verify persisted in DB
        db_value = config_store.get("daemon_port")
        assert db_value == 61000

    def test_set_config_updates_in_memory(
        self, config_registry, config_state: dict[str, DaemonConfig]
    ) -> None:
        """Test set_config updates the in-memory config via config_setter."""
        tool = config_registry.get_tool("set_config")
        result = tool(key="daemon_port", value=61001)

        assert result["success"] is True
        assert config_state["config"].daemon_port == 61001

    def test_set_config_rejects_invalid_value(self, config_registry) -> None:
        """Test set_config rejects values that fail Pydantic validation."""
        tool = config_registry.get_tool("set_config")
        # Port must be 1024-65535
        result = tool(key="daemon_port", value=80)

        assert result["success"] is False
        assert "error" in result

    def test_set_config_nested_key(
        self, config_registry, config_store: ConfigStore, config_state: dict[str, DaemonConfig]
    ) -> None:
        """Test set_config works with nested dotted keys."""
        tool = config_registry.get_tool("set_config")
        result = tool(key="logging.level", value="debug")

        assert result["success"] is True
        assert config_store.get("logging.level") == "debug"
        assert config_state["config"].logging.level == "debug"


class TestListConfigKeys:
    """Tests for list_config_keys tool."""

    def test_list_config_keys_empty(self, config_registry) -> None:
        """Test list_config_keys returns empty list when DB has no keys."""
        tool = config_registry.get_tool("list_config_keys")
        result = tool()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["keys"] == []

    def test_list_config_keys_after_set(self, config_registry, config_store: ConfigStore) -> None:
        """Test list_config_keys returns keys after setting values."""
        config_store.set("daemon_port", 60887)
        config_store.set("conductor.daily_budget_usd", 50.0)

        tool = config_registry.get_tool("list_config_keys")
        result = tool()

        assert result["success"] is True
        assert result["count"] == 2
        assert "daemon_port" in result["keys"]
        assert "conductor.daily_budget_usd" in result["keys"]

    def test_list_config_keys_with_prefix(self, config_registry, config_store: ConfigStore) -> None:
        """Test list_config_keys filters by prefix."""
        config_store.set("conductor.daily_budget_usd", 50.0)
        config_store.set("conductor.warning_threshold", 0.8)
        config_store.set("daemon_port", 60887)

        tool = config_registry.get_tool("list_config_keys")
        result = tool(prefix="conductor")

        assert result["success"] is True
        assert result["count"] == 2
        assert all(k.startswith("conductor") for k in result["keys"])


class TestEnsureDefaults:
    """Tests for ensure_defaults tool."""

    def test_ensure_defaults_populates_missing_keys(
        self, config_registry, config_store: ConfigStore
    ) -> None:
        """Test ensure_defaults inserts Pydantic defaults for missing keys."""
        tool = config_registry.get_tool("ensure_defaults")
        result = tool(section="logging")

        assert result["success"] is True
        assert result["inserted"] > 0
        assert "logging.level" in result["keys_inserted"]

        # Verify persisted in DB
        db_value = config_store.get("logging.level")
        assert db_value == "info"

    def test_ensure_defaults_does_not_overwrite_existing(
        self, config_registry, config_store: ConfigStore
    ) -> None:
        """Test ensure_defaults skips keys that already exist in DB."""
        # Pre-set a custom value
        config_store.set("logging.level", "debug")

        tool = config_registry.get_tool("ensure_defaults")
        result = tool(section="logging")

        assert result["success"] is True
        # Should not have overwritten the existing key
        db_value = config_store.get("logging.level")
        assert db_value == "debug"

        # The pre-set key should not be in keys_inserted
        if result["inserted"] > 0:
            assert "logging.level" not in result["keys_inserted"]

    def test_ensure_defaults_all_present(self, config_registry, config_store: ConfigStore) -> None:
        """Test ensure_defaults reports when all keys are already present."""
        # First call populates
        tool = config_registry.get_tool("ensure_defaults")
        tool(section="logging")

        # Second call should find nothing to insert
        result = tool(section="logging")
        assert result["success"] is True
        assert result["inserted"] == 0
        assert "already present" in result["message"]

    def test_ensure_defaults_invalid_section(self, config_registry) -> None:
        """Test ensure_defaults returns error for nonexistent section."""
        tool = config_registry.get_tool("ensure_defaults")
        result = tool(section="nonexistent_section")

        assert result["success"] is False
        assert "No default keys" in result["error"]


# ===========================================================================
# Helper function tests
# ===========================================================================


class TestConfigKeyToSecretName:
    """Tests for config_key_to_secret_name helper."""

    def test_simple_key(self) -> None:
        assert config_key_to_secret_name("voice.elevenlabs_api_key") == "elevenlabs_api_key"

    def test_nested_key(self) -> None:
        assert config_key_to_secret_name("a.b.c") == "c"

    def test_no_dots(self) -> None:
        assert config_key_to_secret_name("api_key") == "api_key"


class TestIsSecretKeyName:
    """Tests for is_secret_key_name helper."""

    def test_api_key_suffix(self) -> None:
        assert is_secret_key_name("voice.elevenlabs_api_key") is True

    def test_password_suffix(self) -> None:
        assert is_secret_key_name("db.db_password") is True

    def test_access_token_suffix(self) -> None:
        assert is_secret_key_name("oauth.user_access_token") is True

    def test_non_secret_key(self) -> None:
        assert is_secret_key_name("daemon_port") is False

    def test_non_secret_with_key_in_name(self) -> None:
        assert is_secret_key_name("logging.level") is False


# ===========================================================================
# ConfigStore secret methods
# ===========================================================================


class TestConfigStoreSecrets:
    """Tests for ConfigStore secret-aware methods."""

    @pytest.fixture
    def secret_store(self, temp_db: LocalDatabase) -> SecretStore:
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine-12345"):
            return SecretStore(temp_db)

    def test_set_secret_stores_reference(
        self, config_store: ConfigStore, secret_store: SecretStore
    ) -> None:
        """set_secret stores a $secret: reference in config_store."""
        config_store.set_secret("voice.elevenlabs_api_key", "sk-test-123", secret_store)
        raw = config_store.get("voice.elevenlabs_api_key")
        assert raw == "$secret:elevenlabs_api_key"

    def test_set_secret_encrypts_value(
        self, config_store: ConfigStore, secret_store: SecretStore
    ) -> None:
        """set_secret encrypts the actual value in the secrets table."""
        config_store.set_secret("voice.elevenlabs_api_key", "sk-test-123", secret_store)
        decrypted = secret_store.get("elevenlabs_api_key")
        assert decrypted == "sk-test-123"

    def test_set_secret_marks_is_secret(
        self, config_store: ConfigStore, secret_store: SecretStore
    ) -> None:
        """set_secret sets is_secret=1 in config_store."""
        config_store.set_secret("voice.elevenlabs_api_key", "sk-test-123", secret_store)
        keys = config_store.get_secret_keys()
        assert "voice.elevenlabs_api_key" in keys

    def test_get_secret_keys_empty(self, config_store: ConfigStore) -> None:
        """get_secret_keys returns empty list when no secrets exist."""
        assert config_store.get_secret_keys() == []

    def test_get_secret_keys_multiple(
        self, config_store: ConfigStore, secret_store: SecretStore
    ) -> None:
        """get_secret_keys returns all secret keys."""
        config_store.set_secret("a.api_key", "val1", secret_store)
        config_store.set_secret("b.password", "val2", secret_store)
        keys = config_store.get_secret_keys()
        assert sorted(keys) == ["a.api_key", "b.password"]

    def test_clear_secret(self, config_store: ConfigStore, secret_store: SecretStore) -> None:
        """clear_secret removes from both config_store and secrets."""
        config_store.set_secret("voice.elevenlabs_api_key", "sk-test-123", secret_store)
        config_store.clear_secret("voice.elevenlabs_api_key", secret_store)
        assert config_store.get("voice.elevenlabs_api_key") is None
        assert secret_store.get("elevenlabs_api_key") is None
        assert config_store.get_secret_keys() == []

    def test_normal_set_does_not_mark_secret(self, config_store: ConfigStore) -> None:
        """Regular set() keeps is_secret=0."""
        config_store.set("daemon_port", 9999)
        assert config_store.get_secret_keys() == []


# ===========================================================================
# set_config with is_secret
# ===========================================================================


class TestSetConfigSecret:
    """Tests for set_config tool with is_secret parameter."""

    @pytest.fixture
    def config_registry_with_db(
        self,
        temp_db: LocalDatabase,
        config_store: ConfigStore,
        config_state: dict[str, DaemonConfig],
    ) -> InternalToolRegistry:
        """Create a config registry with db for secret support."""
        return create_config_registry(
            config=config_state["config"],
            config_store=config_store,
            config_setter=lambda c: config_state.__setitem__("config", c),
            db=temp_db,
        )

    def test_set_config_secret_encrypts(
        self, config_registry_with_db, config_store: ConfigStore, temp_db: LocalDatabase
    ) -> None:
        """set_config with is_secret=True encrypts the value."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine-12345"):
            tool = config_registry_with_db.get_tool("set_config")
            result = tool(key="voice.elevenlabs_api_key", value="sk-test-456", is_secret=True)

            assert result["success"] is True
            assert result["stored_as"] == "encrypted_secret"
            assert "value" not in result  # Never expose secret in response

            # Verify encrypted in secrets table
            secret_store = SecretStore(temp_db)
            decrypted = secret_store.get("elevenlabs_api_key")
            assert decrypted == "sk-test-456"

            # Verify config_store has reference
            raw = config_store.get("voice.elevenlabs_api_key")
            assert raw == "$secret:elevenlabs_api_key"

    def test_set_config_normal_unchanged(
        self, config_registry_with_db, config_store: ConfigStore
    ) -> None:
        """set_config without is_secret works as before."""
        tool = config_registry_with_db.get_tool("set_config")
        result = tool(key="daemon_port", value=61000)

        assert result["success"] is True
        assert result["value"] == 61000
        assert config_store.get("daemon_port") == 61000
