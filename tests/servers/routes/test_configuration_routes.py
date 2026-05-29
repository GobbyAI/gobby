"""Tests for configuration routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/configuration.py endpoints using
create_http_server() with a real DaemonConfig and real SecretStore
backed by a real temp_db.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
import yaml
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.routes.configuration_prompts import _normalize_variable_spec
from gobby.servers.tool_approvals import DEFAULT_GLOBAL_APPROVAL_RULES
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

FALKOR_REQUIREPASS_KEY = "databases.falkordb.requirepass"
FALKOR_RESTART_HINT_FRAGMENT = "FalkorDB password"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_config() -> DaemonConfig:
    """A real DaemonConfig with defaults."""
    return DaemonConfig()


@pytest.fixture
def temp_db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


@pytest.fixture
def task_manager(temp_db: Any) -> Any:
    return LocalTaskManager(temp_db)


@pytest.fixture
def server(temp_db: Any, real_config: Any, task_manager: Any) -> Any:
    """Create an HTTPServer with real config and database."""
    return create_http_server(
        config=real_config,
        database=temp_db,
        task_manager=task_manager,
    )


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(server.app)


@pytest.fixture
def postgres_task_manager(postgres_db: Any) -> Any:
    return LocalTaskManager(postgres_db)


@pytest.fixture
def postgres_server(postgres_db: Any, real_config: Any, postgres_task_manager: Any) -> Any:
    return create_http_server(
        config=real_config,
        database=postgres_db,
        task_manager=postgres_task_manager,
    )


@pytest.fixture
def postgres_client(postgres_server: Any) -> TestClient:
    return TestClient(postgres_server.app)


def _config_store_row(db: Any, key: str) -> dict[str, object] | None:
    row = db.fetchone(
        "SELECT key, value, source, is_secret, updated_at FROM config_store WHERE key = %s",
        (key,),
    )
    return dict(row) if row is not None else None


def _secret_row(db: Any, name: str = "requirepass") -> dict[str, object] | None:
    row = db.fetchone(
        "SELECT id, name, encrypted_value, category, description, created_at, updated_at "
        "FROM secrets WHERE name = %s",
        (name,),
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# GET /api/config/schema
# ---------------------------------------------------------------------------


class TestGetConfigSchema:
    def test_returns_json_schema(self, client: TestClient) -> None:
        response = client.get("/api/config/schema")
        assert response.status_code == 200
        data = response.json()
        # Real DaemonConfig schema has these
        assert data["type"] == "object"
        assert "properties" in data
        assert "daemon_port" in data["properties"]

    def test_schema_is_stable(self, client: TestClient) -> None:
        """Calling twice returns the same schema."""
        r1 = client.get("/api/config/schema")
        r2 = client.get("/api/config/schema")
        assert r1.json() == r2.json()


# ---------------------------------------------------------------------------
# GET /api/config/values
# ---------------------------------------------------------------------------


class TestGetConfigValues:
    def test_returns_current_config(self, client: TestClient) -> None:
        response = client.get("/api/config/values")
        assert response.status_code == 200
        data = response.json()
        # New shape: {values, secret_keys}
        assert "values" in data
        assert "secret_keys" in data
        assert data["values"]["daemon_port"] == 60887
        assert "websocket" in data["values"]
        assert "web_chat_sandbox" in data["values"]
        assert "agent_sandbox" in data["values"]

    def test_values_contain_expected_keys(
        self, client: TestClient, real_config: DaemonConfig
    ) -> None:
        response = client.get("/api/config/values")
        values = response.json()["values"]
        expected = real_config.model_dump(mode="json", exclude_none=True)
        # All non-secret keys should match
        assert values["daemon_port"] == expected["daemon_port"]
        assert values["websocket"] == expected["websocket"]

    def test_secret_keys_auto_detected(self, client: TestClient) -> None:
        """Keys matching secret patterns are reported in secret_keys."""
        response = client.get("/api/config/values")
        data = response.json()
        assert "auth.password" in data["secret_keys"]

    def test_values_accept_hub_database_protocol(
        self, non_local_hub_db: Any, real_config: DaemonConfig
    ) -> None:
        server = create_http_server(
            config=real_config,
            database=non_local_hub_db,
            task_manager=LocalTaskManager(non_local_hub_db),
        )
        c = TestClient(server.app)

        response = c.get("/api/config/values")

        assert response.status_code == 200
        assert response.json()["values"]["daemon_port"] == real_config.daemon_port


# ---------------------------------------------------------------------------
# PUT /api/config/values
# ---------------------------------------------------------------------------


class TestSaveConfigValues:
    def test_save_valid_values(self, client: TestClient) -> None:
        """Valid partial update succeeds (save_config is patched by conftest)."""
        response = client.put(
            "/api/config/values",
            json={"values": {"daemon_port": 9999}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True

    def test_save_deep_merge(self, client: TestClient) -> None:
        """Deep merge should merge nested dicts, not replace them."""
        response = client.put(
            "/api/config/values",
            json={"values": {"websocket": {"port": 61000}}},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_save_daemon_owned_sandbox_values_to_config_store(
        self, client: TestClient, temp_db: Any
    ) -> None:
        """Daemon-owned sandbox config should persist through the config store."""
        response = client.put(
            "/api/config/values",
            json={
                "values": {
                    "web_chat_sandbox": {
                        "enabled": False,
                        "extra_write_paths": ["/tmp/web-chat-cache"],
                    },
                    "agent_sandbox": {
                        "extra_read_paths": ["/tmp/agent-read"],
                    },
                }
            },
        )

        assert response.status_code == 200
        store = ConfigStore(temp_db)
        assert store.get("web_chat_sandbox.enabled") is False
        assert store.get("web_chat_sandbox.extra_write_paths") == ["/tmp/web-chat-cache"]
        assert store.get("agent_sandbox.extra_read_paths") == ["/tmp/agent-read"]

    def test_save_invalid_values_returns_400(self, client: TestClient) -> None:
        """Invalid config values cause a 400."""
        response = client.put(
            "/api/config/values",
            json={"values": {"ui": {"port": 99999, "mode": "invalid"}}},
        )
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_save_falkordb_requirepass_encrypts_and_masks(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        response = postgres_client.put(
            "/api/config/values",
            json={"values": {"databases": {"falkordb": {"requirepass": "Valid-123"}}}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]

        store = ConfigStore(postgres_db)
        assert store.get(FALKOR_REQUIREPASS_KEY) == "$secret:requirepass"
        assert FALKOR_REQUIREPASS_KEY in store.get_secret_keys()
        assert SecretStore(postgres_db).get("requirepass") == "Valid-123"

        get_response = postgres_client.get("/api/config/values")
        payload = get_response.json()
        assert payload["values"]["databases"]["falkordb"]["requirepass"] == "********"
        assert FALKOR_REQUIREPASS_KEY in payload["secret_keys"]

    def test_save_falkordb_requirepass_invalid_returns_422_without_partial_write(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.put(
            "/api/config/values",
            json={
                "values": {
                    "daemon_port": 9999,
                    "databases": {"falkordb": {"requirepass": "contains space"}},
                }
            },
        )

        assert response.status_code == 422
        body = response.json()
        assert body == {
            "detail": "FalkorDB password must not contain whitespace",
            "key": FALKOR_REQUIREPASS_KEY,
        }

        store = ConfigStore(postgres_db)
        assert store.get("daemon_port") is None
        assert store.get(FALKOR_REQUIREPASS_KEY) is None


# ---------------------------------------------------------------------------
# POST /api/config/values/validate
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_config(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/values/validate",
            json={"values": {"daemon_port": 9999}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["errors"] == []

    def test_invalid_config(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/values/validate",
            json={"values": {"ui": {"port": 99999, "mode": "invalid"}}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0

    def test_empty_values_is_valid(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/values/validate",
            json={"values": {}},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True


# ---------------------------------------------------------------------------
# POST /api/config/values/reset
# ---------------------------------------------------------------------------


class TestResetConfig:
    def test_reset_success(self, client: TestClient, temp_db: Any) -> None:
        """Reset clears config_store and sets in-memory config to defaults."""
        # Seed some config in DB
        store = ConfigStore(temp_db)
        store.set("daemon_port", 9999)
        response = client.post("/api/config/values/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True
        # Verify DB was cleared
        assert store.get_all() == {}

    def test_reset_failure(self, client: TestClient) -> None:
        """Reset failure returns 500."""
        with patch(
            "gobby.servers.routes.configuration_context.ConfigStore.delete_all",
            side_effect=OSError("Permission denied"),
        ):
            response = client.post("/api/config/values/reset")
        assert response.status_code == 500
        assert "Permission denied" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/config/template
# ---------------------------------------------------------------------------


class TestGetTemplate:
    def test_returns_yaml_with_defaults(self, client: TestClient) -> None:
        response = client.get("/api/config/template")
        assert response.status_code == 200
        content = response.json()["content"]
        assert "daemon_port" in content
        assert isinstance(content, str)

    def test_includes_db_overrides(self, client: TestClient, temp_db: Any) -> None:
        """DB overrides are merged into the template."""
        store = ConfigStore(temp_db)
        store.set("daemon_port", 9999)
        response = client.get("/api/config/template")
        assert response.status_code == 200
        assert "9999" in response.json()["content"]

    def test_masks_falkordb_requirepass_secret(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_REQUIREPASS_KEY, "Valid-123", SecretStore(postgres_db))

        response = postgres_client.get("/api/config/template")

        assert response.status_code == 200
        content = response.json()["content"]
        parsed = yaml.safe_load(content)
        assert parsed["databases"]["falkordb"]["requirepass"] == "********"
        assert "$secret:requirepass" not in content
        assert "Valid-123" not in content


# ---------------------------------------------------------------------------
# PUT /api/config/template
# ---------------------------------------------------------------------------


class TestSaveTemplate:
    def test_save_valid_yaml(self, client: TestClient, temp_db: Any) -> None:
        response = client.put(
            "/api/config/template",
            json={"content": "daemon_port: 9999\n"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True
        # Verify the DB has the non-default value
        store = ConfigStore(temp_db)
        assert store.get("daemon_port") == 9999

    def test_save_empty_yaml_treated_as_empty_dict(self, client: TestClient) -> None:
        """Empty YAML (parsed as None) is treated as empty dict."""
        response = client.put(
            "/api/config/template",
            json={"content": ""},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True


# ---------------------------------------------------------------------------
# UI settings + approval rules
# ---------------------------------------------------------------------------


class TestUISettingsPostPlanMode:
    def test_ui_settings_round_trip_includes_post_plan_mode(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.put(
            "/api/config/ui-settings",
            json={
                "fontSize": 18,
                "defaultChatMode": "plan",
                "postPlanChatMode": "bypass",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        get_response = client.get("/api/config/ui-settings")
        assert get_response.status_code == 200
        assert get_response.json()["fontSize"] == 18
        assert get_response.json()["defaultChatMode"] == "plan"
        assert get_response.json()["postPlanChatMode"] == "bypass"

        store = ConfigStore(temp_db)
        assert store.get("ui_settings.postPlanChatMode") == "bypass"


class TestGlobalToolApprovalRules:
    def test_get_global_tool_approval_rules_defaults(self, client: TestClient) -> None:
        response = client.get("/api/config/tool-approvals/global")
        assert response.status_code == 200
        data = response.json()
        assert data["rules"] == list(DEFAULT_GLOBAL_APPROVAL_RULES)
        assert data["default_rules"] == list(DEFAULT_GLOBAL_APPROVAL_RULES)
        assert "mcp:gobby*:*" in data["built_in_exemptions"]

    def test_save_global_tool_approval_rules_normalizes(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.put(
            "/api/config/tool-approvals/global",
            json={
                "rules": [" tool:Write ", "tool:Write", "", "mcp:third-party:*"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["rules"] == ["tool:Write", "mcp:third-party:*"]

        store = ConfigStore(temp_db)
        assert store.get("tool_approvals.global_rules") == ["tool:Write", "mcp:third-party:*"]


class TestValidationDetectionPreview:
    def test_preview_validation_detection_matches_builtin(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/validation-detection/preview",
            json={"command": "cargo clippy --no-default-features -- -D warnings"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["matched"] is True
        assert data["matcher_id"] == "rust-validation"

    def test_preview_validation_detection_uses_supplied_config(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/validation-detection/preview",
            json={
                "command": "./scripts/ci --fast",
                "config": {
                    "builtin_matchers_enabled": False,
                    "custom_matchers": [
                        {
                            "id": "project-ci",
                            "label": "Project CI",
                            "prefixes": ["./scripts/ci"],
                        }
                    ],
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["matched"] is True
        assert data["matcher_id"] == "project-ci"

    def test_save_invalid_yaml_syntax(self, client: TestClient) -> None:
        response = client.put(
            "/api/config/template",
            json={"content": ":\n  :\n    - [invalid"},
        )
        assert response.status_code == 400
        assert "Invalid YAML" in response.json()["detail"]

    def test_save_yaml_not_a_dict(self, client: TestClient) -> None:
        response = client.put(
            "/api/config/template",
            json={"content": "- item1\n- item2\n"},
        )
        assert response.status_code == 400
        assert "mapping" in response.json()["detail"]

    def test_save_yaml_invalid_config(self, client: TestClient) -> None:
        """Valid YAML but invalid DaemonConfig values."""
        response = client.put(
            "/api/config/template",
            json={"content": "ui:\n  port: 99999\n  mode: invalid\n"},
        )
        assert response.status_code == 400

    def test_only_stores_non_defaults(self, client: TestClient, temp_db: Any) -> None:
        """Template save should only store values that differ from defaults."""
        # Save with all defaults except one change
        response = client.put(
            "/api/config/template",
            json={"content": "daemon_port: 7777\nbind_host: localhost\n"},
        )
        assert response.status_code == 200
        store = ConfigStore(temp_db)
        keys = store.list_keys()
        # Only daemon_port should be stored (bind_host is default)
        assert "daemon_port" in keys
        assert "bind_host" not in keys

    def test_save_template_falkordb_requirepass_encrypts(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        content = yaml.safe_dump(
            {
                "databases": {
                    "falkordb": {
                        "requirepass": "Valid-123",
                        "rrf_k": 77,
                    }
                }
            },
            sort_keys=False,
        )

        response = postgres_client.put("/api/config/template", json={"content": content})

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]

        store = ConfigStore(postgres_db)
        assert store.get(FALKOR_REQUIREPASS_KEY) == "$secret:requirepass"
        assert store.get("databases.falkordb.rrf_k") == 77
        assert SecretStore(postgres_db).get("requirepass") == "Valid-123"

    def test_save_template_invalid_falkordb_requirepass_returns_422_without_writes(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        content = yaml.safe_dump(
            {
                "daemon_port": 9999,
                "databases": {"falkordb": {"requirepass": "contains space"}},
            },
            sort_keys=False,
        )

        response = postgres_client.put("/api/config/template", json={"content": content})

        assert response.status_code == 422
        assert response.json() == {
            "detail": "FalkorDB password must not contain whitespace",
            "key": FALKOR_REQUIREPASS_KEY,
        }
        store = ConfigStore(postgres_db)
        assert store.get("daemon_port") is None
        assert store.get(FALKOR_REQUIREPASS_KEY) is None

    def test_save_template_masked_falkordb_requirepass_is_noop(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_REQUIREPASS_KEY, "Valid-123", SecretStore(postgres_db))
        before_config = _config_store_row(postgres_db, FALKOR_REQUIREPASS_KEY)
        before_secret = _secret_row(postgres_db)
        content = yaml.safe_dump(
            {"databases": {"falkordb": {"requirepass": "********"}}},
            sort_keys=False,
        )

        with patch(
            "gobby.servers.routes.configuration_secrets.validate_falkordb_password",
            side_effect=AssertionError("masked sentinel must not be validated"),
            create=True,
        ) as validator:
            response = postgres_client.put("/api/config/template", json={"content": content})

        assert response.status_code == 200
        assert response.json()["requires_restart"] is False
        validator.assert_not_called()
        assert _config_store_row(postgres_db, FALKOR_REQUIREPASS_KEY) == before_config
        assert _secret_row(postgres_db) == before_secret
        assert SecretStore(postgres_db).get("requirepass") == "Valid-123"


# ---------------------------------------------------------------------------
# Secrets endpoints  (GET, POST, DELETE /api/config/secrets)
# ---------------------------------------------------------------------------


class TestSecretsEndpoints:
    def test_list_secrets_empty(self, client: TestClient) -> None:
        with patch("gobby.servers.routes.configuration_context.SecretStore") as mock_cls:
            mock_store = MagicMock(spec=SecretStore)
            mock_store.list.return_value = []
            mock_cls.return_value = mock_store
            response = client.get("/api/config/secrets")
        assert response.status_code == 200
        data = response.json()
        assert data["secrets"] == []
        assert "categories" in data

    def test_list_secrets_with_data(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        """Use a real SecretStore on the temp_db."""
        store = SecretStore(temp_db)
        store.set(name="MY_KEY", plaintext_value="secret123", category="llm", description="Test")
        response = client.get("/api/config/secrets")
        assert response.status_code == 200
        data = response.json()
        assert len(data["secrets"]) >= 1
        names = [s["name"] for s in data["secrets"]]
        assert "my_key" in names

    def test_create_secret(self, client: TestClient, mock_machine_id: Any) -> None:
        response = client.post(
            "/api/config/secrets",
            json={
                "name": "TEST_SECRET",
                "value": "super-secret",
                "category": "general",
                "description": "A test secret",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["secret"]["name"] == "test_secret"

    def test_secret_routes_accept_hub_database_protocol(
        self, non_local_hub_db: Any, real_config: Any, mock_machine_id: Any
    ) -> None:
        server = create_http_server(
            config=real_config,
            database=non_local_hub_db,
            task_manager=LocalTaskManager(non_local_hub_db),
        )
        c = TestClient(server.app)

        create_response = c.post(
            "/api/config/secrets",
            json={"name": "POSTGRES_ONLY", "value": "secret-value", "category": "llm"},
        )
        list_response = c.get("/api/config/secrets")

        assert create_response.status_code == 200
        assert list_response.status_code == 200
        names = {secret["name"] for secret in list_response.json()["secrets"]}
        assert "postgres_only" in names

    def test_create_secret_default_category(self, client: TestClient, mock_machine_id: Any) -> None:
        response = client.post(
            "/api/config/secrets",
            json={"name": "KEY2", "value": "val"},
        )
        assert response.status_code == 200
        assert response.json()["secret"]["category"] == "general"

    def test_create_secret_invalid_category(self, client: TestClient, mock_machine_id: Any) -> None:
        response = client.post(
            "/api/config/secrets",
            json={"name": "KEY3", "value": "val", "category": "bogus"},
        )
        assert response.status_code == 400
        assert "Invalid category" in response.json()["detail"]

    def test_delete_secret_success(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        store = SecretStore(temp_db)
        store.set(name="TO_DELETE", plaintext_value="x")
        response = client.delete("/api/config/secrets/TO_DELETE")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_delete_secret_not_found(self, client: TestClient, mock_machine_id: Any) -> None:
        response = client.delete("/api/config/secrets/NONEXISTENT")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_delete_secret_internal_error(self, client: TestClient) -> None:
        with patch("gobby.servers.routes.configuration_context.SecretStore") as mock_cls:
            mock_store = MagicMock()
            mock_store.delete.side_effect = RuntimeError("DB error")
            mock_cls.return_value = mock_store
            response = client.delete("/api/config/secrets/MY_SECRET")
        assert response.status_code == 500
        assert "DB error" in response.json()["detail"]

    def test_list_secrets_internal_error(self, client: TestClient) -> None:
        with patch("gobby.servers.routes.configuration_context.SecretStore") as mock_cls:
            mock_store = MagicMock()
            mock_store.list.side_effect = RuntimeError("Boom")
            mock_cls.return_value = mock_store
            response = client.get("/api/config/secrets")
        assert response.status_code == 500

    def test_create_secret_internal_error(self, client: TestClient) -> None:
        with patch("gobby.servers.routes.configuration_context.SecretStore") as mock_cls:
            mock_store = MagicMock()
            mock_store.set.side_effect = RuntimeError("Encryption failed")
            mock_cls.return_value = mock_store
            response = client.post(
                "/api/config/secrets",
                json={"name": "KEY", "value": "val"},
            )
        assert response.status_code == 500

    def test_database_not_available(self, real_config: Any) -> None:
        """When database is not a hub adapter, _get_secret_store raises 503."""
        server = create_http_server(
            config=real_config,
            database="not-a-database",
        )
        c = TestClient(server.app)
        response = c.get("/api/config/secrets")
        assert response.status_code == 503
        assert "Database not available" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Prompts endpoints  (GET, GET detail, PUT, DELETE)
# ---------------------------------------------------------------------------


class TestPromptsEndpoints:
    def test_list_prompts(self, client: TestClient) -> None:
        """Lists bundled prompts from real PromptLoader."""
        response = client.get("/api/config/prompts")
        assert response.status_code == 200
        data = response.json()
        assert "prompts" in data
        assert "categories" in data
        assert "total" in data
        assert isinstance(data["total"], int)

    def test_list_prompts_has_categories(self, client: TestClient) -> None:
        """Category counts are populated from listed prompts."""
        response = client.get("/api/config/prompts")
        data = response.json()
        if data["total"] > 0:
            assert len(data["categories"]) > 0
            # Sum of category counts should equal total
            assert sum(data["categories"].values()) == data["total"]

    def test_list_prompts_source_is_bundled(self, client: TestClient) -> None:
        """Without overrides, all sources should be 'bundled'."""
        response = client.get("/api/config/prompts")
        for p in response.json()["prompts"]:
            assert p["source"] == "bundled"
            assert p["has_override"] is False

    def test_get_prompt_detail(self, client: TestClient) -> None:
        """Get detail of a prompt that exists."""
        # First, list to find a valid prompt path
        list_resp = client.get("/api/config/prompts")
        prompts = list_resp.json()["prompts"]
        if not prompts:
            pytest.skip("No bundled prompts found")
        path = prompts[0]["path"]
        response = client.get(f"/api/config/prompts/{path}")
        assert response.status_code == 200
        data = response.json()
        assert data["path"] == path
        assert "content" in data
        assert "variables" in data
        assert data["source"] == "bundled"

    def test_normalize_variable_spec_preserves_api_shape(self) -> None:
        assert _normalize_variable_spec({"type": "int", "required": True, "default": 3}) == {
            "type": "int",
            "required": True,
            "default": 3,
        }
        assert _normalize_variable_spec("fallback") == {
            "type": "str",
            "default": "fallback",
        }

    def test_get_prompt_not_found(self, client: TestClient) -> None:
        response = client.get("/api/config/prompts/nonexistent/prompt")
        assert response.status_code == 404

    def test_list_prompts_error(self, client: TestClient) -> None:
        with patch(
            "gobby.storage.prompts.LocalPromptManager.list_prompts",
            side_effect=RuntimeError("DB broke"),
        ):
            response = client.get("/api/config/prompts")
        assert response.status_code == 500

    def test_save_and_delete_prompt_override(self, client: TestClient) -> None:
        """Save an override, then delete it."""
        # Save
        response = client.put(
            "/api/config/prompts/test/prompt",
            json={"content": "# Custom override\nHello world"},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        # Verify override exists via GET
        detail = client.get("/api/config/prompts/test/prompt")
        assert detail.status_code == 200
        assert detail.json()["source"] == "overridden"

        # Delete
        response = client.delete("/api/config/prompts/test/prompt")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_delete_prompt_override_not_found(self, client: TestClient) -> None:
        response = client.delete("/api/config/prompts/no/such/prompt")
        assert response.status_code == 404
        assert "No override" in response.json()["detail"]

    def test_save_prompt_override_error(self, client: TestClient) -> None:
        with patch(
            "gobby.storage.prompts.LocalPromptManager.create_prompt",
            side_effect=OSError("No space"),
        ):
            response = client.put(
                "/api/config/prompts/test/save_error_prompt",
                json={"content": "# Fail"},
            )
        assert response.status_code == 500

    def test_delete_prompt_override_error(self, client: TestClient) -> None:
        # First create a global override so the route finds it
        client.put(
            "/api/config/prompts/test/delete_error",
            json={"content": "# Override to fail on delete"},
        )
        with patch(
            "gobby.storage.prompts.LocalPromptManager.delete_prompt",
            side_effect=OSError("Locked"),
        ):
            response = client.delete("/api/config/prompts/test/delete_error")
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


class TestExportImport:
    def test_export_config(self, client: TestClient, mock_machine_id: Any) -> None:
        response = client.post("/api/config/export")
        assert response.status_code == 200
        data = response.json()
        assert "exported_at" in data
        assert "config_store" in data
        assert isinstance(data["config_store"], dict)
        assert isinstance(data["prompts"], dict)
        assert isinstance(data["secrets"], list)

    def test_export_config_with_prompt_overrides(
        self, client: TestClient, mock_machine_id: Any
    ) -> None:
        # Insert a global override via the API
        client.put(
            "/api/config/prompts/expansion/system",
            json={"content": "# Custom"},
        )
        response = client.post("/api/config/export")
        data = response.json()
        assert "expansion/system.md" in data["prompts"]
        assert "# Custom" in data["prompts"]["expansion/system.md"]

    def test_import_config_store(self, client: TestClient, temp_db: Any) -> None:
        """Import flat config_store dict writes to DB."""
        response = client.post(
            "/api/config/import",
            json={"config_store": {"daemon_port": 9999}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "config restored" in data["summary"]
        assert data["requires_restart"] is True
        # Verify DB
        store = ConfigStore(temp_db)
        assert store.get("daemon_port") == 9999

    def test_import_legacy_config(self, client: TestClient, temp_db: Any) -> None:
        """Legacy nested config dict is flattened and stored to DB."""
        response = client.post(
            "/api/config/import",
            json={"config": {"daemon_port": 8888}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "config restored" in data["summary"]
        assert data["requires_restart"] is True

    def test_import_config_with_prompts(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/import",
            json={
                "prompts": {"expansion/system.md": "# Custom prompt override"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "1 prompt override(s) restored" in data["summary"]
        assert data["requires_restart"] is False

    def test_import_nothing(self, client: TestClient) -> None:
        response = client.post("/api/config/import", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["summary"] == "nothing to import"
        assert data["requires_restart"] is False

    def test_import_invalid_config(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/import",
            json={"config": {"ui": {"port": 99999, "mode": "invalid"}}},
        )
        assert response.status_code == 400

    def test_import_database_error_returns_400(self, client: TestClient) -> None:
        with patch.object(
            ConfigStore,
            "delete_all",
            side_effect=psycopg.OperationalError("database unavailable"),
        ):
            response = client.post(
                "/api/config/import",
                json={"config_store": {"daemon_port": 9999}},
            )

        assert response.status_code == 400
        assert "database unavailable" in response.json()["detail"]

    def test_import_non_string_secret_value_fails_before_deleting_existing_config(
        self, client: TestClient, temp_db: Any
    ) -> None:
        store = ConfigStore(temp_db)
        store.set("daemon_port", 5555)

        response = client.post(
            "/api/config/import",
            json={
                "config_store": {
                    "daemon_port": 9999,
                    "service.provider_api_key": {"nested": "bad"},
                }
            },
        )

        assert response.status_code == 400
        assert "Secret 'service.provider_api_key' must be a string" in response.json()["detail"]
        assert store.get("daemon_port") == 5555
        assert store.get("service.provider_api_key") is None

    def test_import_config_and_prompts_together(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/import",
            json={
                "config_store": {"daemon_port": 7777},
                "prompts": {"test/foo.md": "# Foo"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "config restored" in data["summary"]
        assert "1 prompt override(s) restored" in data["summary"]
        assert data["requires_restart"] is True

    def test_import_config_store_falkordb_requirepass_encrypts(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config_store": {
                    FALKOR_REQUIREPASS_KEY: "Valid-123",
                    "databases.falkordb.rrf_k": 77,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]

        store = ConfigStore(postgres_db)
        assert store.get(FALKOR_REQUIREPASS_KEY) == "$secret:requirepass"
        assert store.get("databases.falkordb.rrf_k") == 77
        assert SecretStore(postgres_db).get("requirepass") == "Valid-123"

    def test_import_legacy_config_falkordb_requirepass_encrypts(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config": {
                    "databases": {
                        "falkordb": {
                            "requirepass": "Valid-123",
                            "rrf_k": 77,
                        }
                    }
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]

        store = ConfigStore(postgres_db)
        assert store.get(FALKOR_REQUIREPASS_KEY) == "$secret:requirepass"
        assert store.get("databases.falkordb.rrf_k") == 77
        assert SecretStore(postgres_db).get("requirepass") == "Valid-123"

    def test_import_falkordb_secret_reference_preserves_secret_row(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_REQUIREPASS_KEY, "Valid-123", SecretStore(postgres_db))
        before_secret = _secret_row(postgres_db)

        with patch(
            "gobby.servers.routes.configuration_secrets.validate_falkordb_password",
            side_effect=AssertionError("secret references must not be validated"),
            create=True,
        ) as validator:
            response = postgres_client.post(
                "/api/config/import",
                json={
                    "config_store": {
                        FALKOR_REQUIREPASS_KEY: "$secret:requirepass",
                        "daemon_port": 9999,
                    },
                    "config_secret_keys": [FALKOR_REQUIREPASS_KEY],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]
        validator.assert_not_called()
        assert _secret_row(postgres_db) == before_secret
        assert store.get(FALKOR_REQUIREPASS_KEY) == "$secret:requirepass"
        assert FALKOR_REQUIREPASS_KEY in store.get_secret_keys()
        assert store.get("daemon_port") == 9999

    def test_export_then_import_preserves_requirepass_secret_row(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_REQUIREPASS_KEY, "Valid-123", SecretStore(postgres_db))
        before_secret = _secret_row(postgres_db)

        export_response = postgres_client.post("/api/config/export")
        assert export_response.status_code == 200
        bundle = export_response.json()

        with patch(
            "gobby.servers.routes.configuration_secrets.validate_falkordb_password",
            side_effect=AssertionError("exported secret refs must not be validated"),
            create=True,
        ) as validator:
            import_response = postgres_client.post(
                "/api/config/import",
                json={
                    "config_store": bundle["config_store"],
                    "config_secret_keys": bundle["config_secret_keys"],
                },
            )

        assert import_response.status_code == 200
        validator.assert_not_called()
        assert _secret_row(postgres_db) == before_secret
        assert store.get(FALKOR_REQUIREPASS_KEY) == "$secret:requirepass"


# ---------------------------------------------------------------------------
# _deep_merge helper
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_deep_merge_basic(self) -> None:
        from gobby.config.app import deep_merge

        base = {"a": 1, "b": {"c": 2, "d": 3}}
        updates = {"b": {"c": 99, "e": 5}, "f": 6}
        deep_merge(base, updates)
        assert base == {"a": 1, "b": {"c": 99, "d": 3, "e": 5}, "f": 6}

    def test_deep_merge_replace_non_dict(self) -> None:
        from gobby.config.app import deep_merge

        base: dict[str, Any] = {"a": {"b": 1}}
        updates = {"a": "string"}
        deep_merge(base, updates)
        assert base == {"a": "string"}

    def test_deep_merge_empty_updates(self) -> None:
        from gobby.config.app import deep_merge

        base = {"a": 1}
        deep_merge(base, {})
        assert base == {"a": 1}

    def test_deep_merge_nested_three_levels(self) -> None:
        from gobby.config.app import deep_merge

        base = {"a": {"b": {"c": 1, "d": 2}}}
        updates = {"a": {"b": {"c": 99}}}
        deep_merge(base, updates)
        assert base == {"a": {"b": {"c": 99, "d": 2}}}


# ---------------------------------------------------------------------------
# Secret-aware config (GET masking, PUT interception)
# ---------------------------------------------------------------------------


class TestSecretAwareConfig:
    """Tests for secret masking in GET /values and encryption in PUT /values."""

    def test_get_values_masks_auto_detected_secrets(self, client: TestClient) -> None:
        """Auto-detected secret keys (like auth.password) are masked."""
        response = client.get("/api/config/values")
        data = response.json()
        assert "auth.password" in data["secret_keys"]

    def test_put_secret_value_encrypts(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        """PUT with a secret-pattern key encrypts via SecretStore."""
        response = client.put(
            "/api/config/values",
            json={"values": {"service": {"provider_api_key": "sk-test-789"}}},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # Verify the config_store has the $secret: reference
        store = ConfigStore(temp_db)
        raw = store.get("service.provider_api_key")
        assert raw is not None
        assert raw.startswith("$secret:")

        # Verify it's flagged as secret
        assert "service.provider_api_key" in store.get_secret_keys()

        # Verify the actual value is encrypted in secrets table
        secret_store = SecretStore(temp_db)
        decrypted = secret_store.get("provider_api_key")
        assert decrypted == "sk-test-789"

    def test_put_masked_value_skipped(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        """PUT with '********' for a secret key skips the update."""
        # First set a secret
        store = ConfigStore(temp_db)
        secret_store = SecretStore(temp_db)
        store.set_secret("service.provider_api_key", "sk-original", secret_store)

        # Now PUT with masked value
        response = client.put(
            "/api/config/values",
            json={"values": {"service": {"provider_api_key": "********"}}},
        )
        assert response.status_code == 200

        # Original secret should be unchanged
        decrypted = secret_store.get("provider_api_key")
        assert decrypted == "sk-original"

    def test_put_empty_secret_clears(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        """PUT with empty string for a secret key clears it."""
        store = ConfigStore(temp_db)
        secret_store = SecretStore(temp_db)
        store.set_secret("service.provider_api_key", "sk-to-delete", secret_store)

        response = client.put(
            "/api/config/values",
            json={"values": {"service": {"provider_api_key": ""}}},
        )
        assert response.status_code == 200

        # Secret should be cleared
        assert store.get("service.provider_api_key") is None
        assert secret_store.get("provider_api_key") is None

    def test_get_values_masks_set_secret(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        """Secrets set via PUT are masked in subsequent GET."""
        # Set a secret
        client.put(
            "/api/config/values",
            json={"values": {"auth": {"password": "my-secret-pw"}}},
        )

        # GET should show masked value
        response = client.get("/api/config/values")
        data = response.json()
        assert data["values"]["auth"]["password"] == "********"

    def test_export_includes_config_secret_keys(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        """Export bundle includes config_secret_keys list."""
        store = ConfigStore(temp_db)
        secret_store = SecretStore(temp_db)
        store.set_secret("service.provider_api_key", "sk-export", secret_store)

        response = client.post("/api/config/export")
        assert response.status_code == 200
        data = response.json()
        assert "config_secret_keys" in data
        assert "service.provider_api_key" in data["config_secret_keys"]

    def test_import_restores_secret_flags(self, client: TestClient, temp_db: Any) -> None:
        """Import with config_secret_keys restores is_secret flags."""
        response = client.post(
            "/api/config/import",
            json={
                "config_store": {
                    "daemon_port": 9999,
                    "service.provider_api_key": "$secret:provider_api_key",
                },
                "config_secret_keys": ["service.provider_api_key"],
            },
        )
        assert response.status_code == 200

        store = ConfigStore(temp_db)
        assert "service.provider_api_key" in store.get_secret_keys()


# ---------------------------------------------------------------------------
# UI Settings (GET, PUT /api/config/ui-settings)
# ---------------------------------------------------------------------------


class TestUISettings:
    def test_get_empty(self, client: TestClient) -> None:
        """No UI settings stored yet returns empty dict."""
        response = client.get("/api/config/ui-settings")
        assert response.status_code == 200
        assert response.json() == {}

    def test_put_and_get(self, client: TestClient) -> None:
        """PUT settings then GET them back."""
        put_resp = client.put(
            "/api/config/ui-settings",
            json={
                "fontSize": 18,
                "model": "sonnet",
                "theme": "light",
                "defaultChatMode": "act",
                "selectedProvider": "codex",
            },
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["ok"] is True

        get_resp = client.get("/api/config/ui-settings")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["fontSize"] == 18
        assert data["model"] == "sonnet"
        assert data["theme"] == "light"
        assert data["defaultChatMode"] == "act"
        assert data["selectedProvider"] == "codex"

    def test_put_partial_update(self, client: TestClient) -> None:
        """PUT with only some keys updates only those."""
        client.put("/api/config/ui-settings", json={"fontSize": 14, "theme": "dark"})
        client.put("/api/config/ui-settings", json={"fontSize": 20})

        data = client.get("/api/config/ui-settings").json()
        assert data["fontSize"] == 20
        assert data["theme"] == "dark"

    def test_put_empty_body(self, client: TestClient) -> None:
        """PUT with no fields is a no-op."""
        response = client.put("/api/config/ui-settings", json={})
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_ui_settings_isolated_from_daemon_config(
        self, client: TestClient, temp_db: Any
    ) -> None:
        """UI settings use a separate namespace and don't affect DaemonConfig."""
        client.put("/api/config/ui-settings", json={"fontSize": 22})

        # Verify stored under ui_settings. prefix
        store = ConfigStore(temp_db)
        assert store.get("ui_settings.fontSize") == 22
        # Not in the main config namespace
        assert store.get("fontSize") is None

    def test_delete_existing_setting(self, client: TestClient) -> None:
        """DELETE removes a single UI setting."""
        client.put("/api/config/ui-settings", json={"fontSize": 16, "theme": "dark"})

        resp = client.delete("/api/config/ui-settings/fontSize")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # fontSize gone, theme still present
        data = client.get("/api/config/ui-settings").json()
        assert "fontSize" not in data
        assert data["theme"] == "dark"

    def test_delete_not_found(self, client: TestClient) -> None:
        """DELETE for a valid key that was never set returns 404."""
        resp = client.delete("/api/config/ui-settings/theme")
        assert resp.status_code == 404

    def test_delete_invalid_key(self, client: TestClient) -> None:
        """DELETE with an unknown key returns 400."""
        resp = client.delete("/api/config/ui-settings/bogusKey")
        assert resp.status_code == 400
        assert "Unknown UI setting" in resp.json()["detail"]
