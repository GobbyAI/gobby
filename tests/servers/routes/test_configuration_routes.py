"""Tests for configuration routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/configuration.py endpoints using
create_http_server() with a real DaemonConfig and real SecretStore
backed by a real temp_db.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
import yaml
from fastapi import APIRouter, FastAPI
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_MODEL_KEY,
    EMBEDDING_API_KEY_SECRET_NAME,
)
from gobby.prompts.sync import sync_bundled_prompts
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.configuration_import_export import _prompt_export_key
from gobby.servers.routes.configuration_models import SaveUISettingsRequest
from gobby.servers.routes.configuration_prompts import _normalize_variable_spec
from gobby.servers.routes.configuration_secrets import MASKED_SECRET
from gobby.servers.routes.configuration_ui_settings import UI_SETTINGS_KEYS
from gobby.servers.routes.configuration_values import register_value_routes
from gobby.servers.tool_approvals import DEFAULT_GLOBAL_APPROVAL_RULES
from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    PASSWORD_HASH_KEY,
    USERNAME_KEY,
    hash_password,
    hash_token,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.prompts import LocalPromptManager
from gobby.storage.secrets import SecretStore
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

FALKOR_PASSWORD_KEY = "databases.falkordb.password"
FALKOR_RESTART_HINT_FRAGMENT = "FalkorDB password"
LOCAL_RUNTIME_TOKEN = "configuration-route-test-token"


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
def server(temp_db: Any, real_config: Any, task_manager: Any, tmp_path: Any) -> Any:
    """Create an HTTPServer with real config and database."""
    ConfigStore(temp_db).set(
        LOCAL_API_TOKEN_HASH_KEY,
        hash_token(LOCAL_RUNTIME_TOKEN),
        source="system",
    )
    http_server = create_http_server(
        config=real_config,
        database=temp_db,
        task_manager=task_manager,
    )
    http_server.auth_service = AuthService(
        lambda: temp_db,
        mode="disabled",
        token_file=tmp_path / "local_cli_token",
    )
    return http_server


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(
        server.app,
        headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
    )


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


def _secret_row(db: Any, name: str = "falkordb_password") -> dict[str, object] | None:
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

    def test_removed_auth_password_is_not_exposed(self, client: TestClient) -> None:
        response = client.get("/api/config/values")
        data = response.json()
        assert "password" not in data["values"]["auth"]
        assert "session_secret" not in data["values"]["auth"]
        assert "auth.password" not in data["secret_keys"]

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
        """Valid partial update succeeds with config export patched by conftest."""
        response = client.put(
            "/api/config/values",
            json={"values": {"daemon_port": 9999}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True

    def test_save_voice_audio_plaintext_api_key_is_rejected_before_storage(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.put(
            "/api/config/values",
            json={
                "values": {
                    "voice": {
                        "openai_compatible_audio": [
                            {
                                "provider": "remote-stt",
                                "url": "https://audio.example/v1",
                                "model": "whisper-large-v3",
                                "api_key": "plaintext-key",
                            }
                        ]
                    }
                }
            },
        )

        assert response.status_code == 400
        assert "$secret:NAME" in response.json()["detail"]
        assert ConfigStore(temp_db).get("voice.openai_compatible_audio") is None

    def test_save_voice_audio_secret_reference_persists_resolves_and_masks(
        self,
        client: TestClient,
        temp_db: Any,
        server: Any,
        mock_machine_id: Any,
    ) -> None:
        SecretStore(temp_db).set("remote_stt_api_key", "resolved-key")
        binding = {
            "provider": "remote-stt",
            "url": "https://audio.example/v1",
            "model": "whisper-large-v3",
            "api_key": "$secret:REMOTE_STT_API_KEY",
        }

        response = client.put(
            "/api/config/values",
            json={"values": {"voice": {"openai_compatible_audio": [binding]}}},
        )

        assert response.status_code == 200
        stored = ConfigStore(temp_db).get("voice.openai_compatible_audio")
        assert stored == [binding]
        runtime_binding = server.services.config.voice.openai_compatible_audio[0]
        assert runtime_binding.api_key == "resolved-key"

        values = client.get("/api/config/values").json()["values"]
        exposed_binding = values["voice"]["openai_compatible_audio"][0]
        assert exposed_binding["api_key"] == MASKED_SECRET
        assert exposed_binding["url"] == "https://audio.example/v1"
        assert "resolved-key" not in str(values)

        exposed_binding["model"] = "whisper-v4"
        round_trip = client.put(
            "/api/config/values",
            json={"values": {"voice": {"openai_compatible_audio": [exposed_binding]}}},
        )
        assert round_trip.status_code == 200
        stored_after_round_trip = ConfigStore(temp_db).get("voice.openai_compatible_audio")
        assert stored_after_round_trip[0]["api_key"] == "$secret:REMOTE_STT_API_KEY"
        assert stored_after_round_trip[0]["model"] == "whisper-v4"
        assert server.services.config.voice.openai_compatible_audio[0].api_key == "resolved-key"

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

    def test_save_falkordb_password_encrypts_and_masks(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        response = postgres_client.put(
            "/api/config/values",
            json={"values": {"databases": {"falkordb": {"password": "Valid-123"}}}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]

        store = ConfigStore(postgres_db)
        assert store.get(FALKOR_PASSWORD_KEY) == "$secret:falkordb_password"
        assert FALKOR_PASSWORD_KEY in store.get_secret_keys()
        assert SecretStore(postgres_db).get("falkordb_password") == "Valid-123"

        get_response = postgres_client.get("/api/config/values")
        payload = get_response.json()
        assert payload["values"]["databases"]["falkordb"]["password"] == "********"
        assert FALKOR_PASSWORD_KEY in payload["secret_keys"]

    def test_save_falkordb_password_invalid_returns_422_without_partial_write(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.put(
            "/api/config/values",
            json={
                "values": {
                    "daemon_port": 9999,
                    "databases": {"falkordb": {"password": "contains space"}},
                }
            },
        )

        assert response.status_code == 422
        body = response.json()
        assert body == {
            "detail": "FalkorDB password must not contain whitespace",
            "key": FALKOR_PASSWORD_KEY,
        }

        store = ConfigStore(postgres_db)
        assert store.get("daemon_port") is None
        assert store.get(FALKOR_PASSWORD_KEY) is None

    def test_save_falkordb_requirepass_is_rejected(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.put(
            "/api/config/values",
            json={
                "values": {
                    "daemon_port": 9999,
                    "databases": {"falkordb": {"requirepass": "Valid-123"}},
                }
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid configuration values"
        store = ConfigStore(postgres_db)
        assert store.get("daemon_port") is None
        assert store.get(FALKOR_PASSWORD_KEY) is None


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

    def test_voice_audio_plaintext_api_key_is_invalid(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/values/validate",
            json={
                "values": {
                    "voice": {
                        "openai_compatible_audio": [
                            {
                                "provider": "remote-stt",
                                "url": "https://audio.example/v1",
                                "model": "whisper-large-v3",
                                "api_key": "plaintext-key",
                            }
                        ]
                    }
                }
            },
        )

        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert "$secret:NAME" in response.json()["errors"][0]

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

    def test_masked_secret_placeholder_is_ignored(self, client: TestClient) -> None:
        response = client.post(
            "/api/config/values/validate",
            json={"values": {"databases": {"falkordb": {"password": MASKED_SECRET}}}},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True


# ---------------------------------------------------------------------------
# POST /api/config/values/reset
# ---------------------------------------------------------------------------


class TestResetConfig:
    def test_reset_success(
        self,
        client: TestClient,
        temp_db: Any,
        mock_machine_id: Any,
    ) -> None:
        """Reset clears config rows and only their encrypted secrets."""
        # Seed some config in DB
        store = ConfigStore(temp_db)
        store.set("daemon_port", 9999)
        secret_store = SecretStore(temp_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", secret_store)
        secret_store.set("independent_token", "keep-me")
        response = client.post("/api/config/values/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["requires_restart"] is True
        # Verify DB was cleared
        assert store.get_all() == {}
        assert secret_store.get("falkordb_password") is None
        assert secret_store.get("independent_token") == "keep-me"

    def test_reset_failure(self, client: TestClient) -> None:
        """Reset failure returns 500."""
        with patch(
            "gobby.servers.routes.configuration_context.ConfigStore.delete_all",
            side_effect=OSError("Permission denied"),
        ):
            response = client.post("/api/config/values/reset")
        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to reset configuration"


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

    def test_masks_voice_audio_api_key(self, client: TestClient, temp_db: Any) -> None:
        ConfigStore(temp_db).set(
            "voice.openai_compatible_audio",
            [
                {
                    "provider": "remote-stt",
                    "url": "https://audio.example/v1",
                    "model": "whisper-large-v3",
                    "api_key": "$secret:REMOTE_STT_API_KEY",
                }
            ],
        )

        response = client.get("/api/config/template")

        assert response.status_code == 200
        values = yaml.safe_load(response.json()["content"])
        binding = values["voice"]["openai_compatible_audio"][0]
        assert binding["api_key"] == MASKED_SECRET
        assert binding["url"] == "https://audio.example/v1"

    def test_includes_db_overrides(self, client: TestClient, temp_db: Any) -> None:
        """DB overrides are merged into the template."""
        store = ConfigStore(temp_db)
        store.set("daemon_port", 9999)
        response = client.get("/api/config/template")
        assert response.status_code == 200
        assert "9999" in response.json()["content"]

    def test_masks_falkordb_password_secret(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", SecretStore(postgres_db))

        response = postgres_client.get("/api/config/template")

        assert response.status_code == 200
        content = response.json()["content"]
        parsed = yaml.safe_load(content)
        assert parsed["databases"]["falkordb"]["password"] == "********"
        assert "$secret:falkordb_password" not in content
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

    def test_save_template_rejects_voice_audio_plaintext_before_storage(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.put(
            "/api/config/template",
            json={
                "content": (
                    "voice:\n"
                    "  openai_compatible_audio:\n"
                    "    - provider: remote-stt\n"
                    "      url: https://audio.example/v1\n"
                    "      model: whisper-large-v3\n"
                    "      api_key: plaintext-key\n"
                )
            },
        )

        assert response.status_code == 400
        assert "$secret:NAME" in response.json()["detail"]
        assert ConfigStore(temp_db).get("voice.openai_compatible_audio") is None

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


class TestUISettingsRoundTrip:
    def test_ui_settings_round_trip_persists_known_keys(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.put(
            "/api/config/ui-settings",
            json={
                "fontSize": 18,
                "defaultChatMode": "plan",
                "planPendingVariant": "amber",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        get_response = client.get("/api/config/ui-settings")
        assert get_response.status_code == 200
        assert get_response.json()["fontSize"] == 18
        assert get_response.json()["defaultChatMode"] == "plan"
        assert get_response.json()["planPendingVariant"] == "amber"

        store = ConfigStore(temp_db)
        assert store.get("ui_settings.defaultChatMode") == "plan"
        assert store.get("ui_settings.planPendingVariant") == "amber"

    def test_ui_settings_rejects_retired_post_plan_mode_key(
        self, client: TestClient, temp_db: HubDatabase
    ) -> None:
        """The post-plan-mode preference was removed; mode is chosen at approval."""
        response = client.put(
            "/api/config/ui-settings",
            json={"postPlanChatMode": "bypass"},
        )
        # Unknown field is ignored, leaving an all-null payload the validator rejects.
        assert response.status_code == 422

        get_response = client.get("/api/config/ui-settings")
        assert get_response.status_code == 200
        assert "postPlanChatMode" not in get_response.json()
        assert ConfigStore(temp_db).get("ui_settings.postPlanChatMode") is None


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

    def test_save_template_embedding_runtime_shape_persists_canonical(
        self, client: TestClient, temp_db: Any
    ) -> None:
        content = yaml.safe_dump({"embeddings": {"model": "bge-m3", "dim": 1024}})

        response = client.put("/api/config/template", json={"content": content})

        assert response.status_code == 200
        store = ConfigStore(temp_db)
        assert store.get(AI_EMBEDDING_MODEL_KEY) == "bge-m3"
        assert store.get("ai.embeddings.dim") == 1024
        assert store.get("embeddings.model") is None

    def test_save_template_falkordb_password_encrypts(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        content = yaml.safe_dump(
            {
                "databases": {
                    "falkordb": {
                        "password": "Valid-123",
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
        assert store.get(FALKOR_PASSWORD_KEY) == "$secret:falkordb_password"
        assert store.get("databases.falkordb.rrf_k") == 77
        assert SecretStore(postgres_db).get("falkordb_password") == "Valid-123"

    def test_save_template_invalid_falkordb_password_returns_422_without_writes(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        content = yaml.safe_dump(
            {
                "daemon_port": 9999,
                "databases": {"falkordb": {"password": "contains space"}},
            },
            sort_keys=False,
        )

        response = postgres_client.put("/api/config/template", json={"content": content})

        assert response.status_code == 422
        assert response.json() == {
            "detail": "FalkorDB password must not contain whitespace",
            "key": FALKOR_PASSWORD_KEY,
        }
        store = ConfigStore(postgres_db)
        assert store.get("daemon_port") is None
        assert store.get(FALKOR_PASSWORD_KEY) is None

    def test_save_template_masked_falkordb_password_is_noop(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", SecretStore(postgres_db))
        before_config = _config_store_row(postgres_db, FALKOR_PASSWORD_KEY)
        before_secret = _secret_row(postgres_db)
        content = yaml.safe_dump(
            {"databases": {"falkordb": {"password": "********"}}},
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
        assert _config_store_row(postgres_db, FALKOR_PASSWORD_KEY) == before_config
        assert _secret_row(postgres_db) == before_secret
        assert SecretStore(postgres_db).get("falkordb_password") == "Valid-123"

    def test_save_template_removes_only_obsolete_config_backed_secrets(
        self,
        postgres_client: TestClient,
        postgres_db: Any,
        mock_machine_id: Any,
    ) -> None:
        store = ConfigStore(postgres_db)
        secret_store = SecretStore(postgres_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", secret_store)
        secret_store.set("independent_token", "keep-me")
        content = yaml.safe_dump({"daemon_port": 7777})

        response = postgres_client.put("/api/config/template", json={"content": content})

        assert response.status_code == 200
        assert store.get(FALKOR_PASSWORD_KEY) is None
        assert secret_store.get("falkordb_password") is None
        assert secret_store.get("independent_token") == "keep-me"

    def test_save_template_rolls_back_when_config_secret_cleanup_fails(
        self,
        postgres_client: TestClient,
        postgres_db: Any,
        mock_machine_id: Any,
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", SecretStore(postgres_db))
        before_config = _config_store_row(postgres_db, FALKOR_PASSWORD_KEY)
        before_secret = _secret_row(postgres_db)
        content = yaml.safe_dump({"daemon_port": 7777})

        with patch.object(
            SecretStore,
            "delete",
            side_effect=RuntimeError("injected secret deletion failure"),
        ):
            response = postgres_client.put("/api/config/template", json={"content": content})

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to save config template"
        assert _config_store_row(postgres_db, FALKOR_PASSWORD_KEY) == before_config
        assert _secret_row(postgres_db) == before_secret
        assert store.get("daemon_port") is None


# ---------------------------------------------------------------------------
# Secrets endpoints  (GET, POST, DELETE /api/config/secrets)
# ---------------------------------------------------------------------------


class TestSecretsEndpoints:
    def test_mutations_require_local_token_when_web_login_is_unconfigured(
        self, server: Any, mock_machine_id: Any
    ) -> None:
        assert server.auth_service.enabled is False
        assert server.services.config.bind_host == "localhost"
        assert server.auth_service.credentials_configured is False
        unauthenticated = TestClient(server.app)

        create_response = unauthenticated.post(
            "/api/config/secrets",
            json={"name": "PROTECTED", "value": "secret"},
        )
        invalid_response = unauthenticated.delete(
            "/api/config/secrets/PROTECTED",
            headers={"Authorization": "Bearer invalid-token"},
        )
        authorized_create = unauthenticated.post(
            "/api/config/secrets",
            headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
            json={"name": "PROTECTED", "value": "secret"},
        )
        authorized_delete = unauthenticated.delete(
            "/api/config/secrets/PROTECTED",
            headers={"Authorization": f"Bearer {LOCAL_RUNTIME_TOKEN}"},
        )

        assert create_response.status_code == 401
        assert invalid_response.status_code == 401
        assert authorized_create.status_code == 200
        assert authorized_delete.status_code == 200

    def test_mutations_accept_configured_web_session(
        self, server: Any, temp_db: Any, tmp_path: Any, mock_machine_id: Any
    ) -> None:
        ConfigStore(temp_db).set_many(
            {
                USERNAME_KEY: "admin",
                PASSWORD_HASH_KEY: hash_password("correct horse battery staple"),
            },
            source="system",
        )
        server.auth_service = AuthService(
            lambda: temp_db,
            mode="required",
            token_file=tmp_path / "configured_auth_token",
        )
        assert server.auth_service.enabled is True
        assert server.auth_service.credentials_configured is True
        browser = TestClient(server.app)

        login_response = browser.post(
            "/api/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        create_response = browser.post(
            "/api/config/secrets",
            json={"name": "SESSION_PROTECTED", "value": "secret"},
        )
        delete_response = browser.delete("/api/config/secrets/SESSION_PROTECTED")

        assert login_response.status_code == 200
        assert create_response.status_code == 200
        assert delete_response.status_code == 200

    def test_non_loopback_bind_refuses_unauthenticated_mutation(
        self, temp_db: Any, task_manager: Any, tmp_path: Any
    ) -> None:
        http_server = create_http_server(
            config=DaemonConfig(bind_host="0.0.0.0", auth_mode="disabled"),
            database=temp_db,
            task_manager=task_manager,
            auth_mode="disabled",
        )
        http_server.auth_service = AuthService(
            lambda: temp_db,
            mode="disabled",
            token_file=tmp_path / "non_loopback_token",
        )
        assert http_server.auth_service.enabled is False
        assert http_server.services.config.bind_host == "0.0.0.0"

        response = TestClient(http_server.app).post(
            "/api/config/secrets",
            json={"name": "REMOTE_WRITE", "value": "secret"},
        )

        assert response.status_code == 401

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
        self, non_local_hub_db: Any, real_config: Any, tmp_path: Any, mock_machine_id: Any
    ) -> None:
        ConfigStore(non_local_hub_db).set(
            LOCAL_API_TOKEN_HASH_KEY,
            hash_token(LOCAL_RUNTIME_TOKEN),
            source="system",
        )
        server = create_http_server(
            config=real_config,
            database=non_local_hub_db,
            task_manager=LocalTaskManager(non_local_hub_db),
        )
        server.auth_service = AuthService(
            lambda: non_local_hub_db,
            mode="disabled",
            token_file=tmp_path / "non_local_token",
        )
        c = TestClient(
            server.app,
            headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
        )

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
        assert response.json()["detail"] == "Internal server error"

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
    @pytest.fixture(autouse=True)
    def _seed_bundled_prompts(self, temp_db: HubDatabase) -> None:
        sync_bundled_prompts(temp_db)

    def test_list_prompts(self, client: TestClient) -> None:
        """Lists bundled prompts from real PromptLoader."""
        response = client.get("/api/config/prompts")
        assert response.status_code == 200
        data = response.json()
        assert "prompts" in data
        assert "categories" in data
        assert "total" in data
        assert isinstance(data["total"], int)
        assert data["limit"] == 500
        assert data["offset"] == 0
        assert data["count"] == len(data["prompts"])

    def test_list_prompts_has_categories(self, client: TestClient) -> None:
        """Category counts are populated from listed prompts."""
        response = client.get("/api/config/prompts")
        data = response.json()
        if data["total"] > 0:
            assert len(data["categories"]) > 0
            # Sum of category counts should equal total
            assert sum(data["categories"].values()) == data["total"]

    def test_list_prompts_clamps_limit(self, client: TestClient) -> None:
        response = client.get("/api/config/prompts?limit=2000&offset=1")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 1000
        assert data["offset"] == 1
        assert data["count"] == len(data["prompts"])

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
        assert prompts
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
            "required": False,
            "default": "fallback",
        }
        assert _normalize_variable_spec(None) == {
            "type": "str",
            "required": False,
            "default": None,
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

    def test_save_prompt_override_uses_project_scope(
        self,
        temp_db: Any,
        real_config: Any,
        task_manager: Any,
        sample_project: dict[str, Any],
    ) -> None:
        project_id = sample_project["id"]
        server = create_http_server(
            config=real_config,
            database=temp_db,
            task_manager=task_manager,
            project_id=project_id,
        )
        scoped_client = TestClient(server.app)

        response = scoped_client.put(
            "/api/config/prompts/test/project",
            json={"content": "# Project override"},
        )

        assert response.status_code == 200
        override = LocalPromptManager(temp_db).get_override("test/project", project_id=project_id)
        assert override is not None
        assert override.scope == "project"
        assert override.project_id == project_id

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

    def test_export_config_preserves_voice_audio_refs_and_masks_legacy_plaintext(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(temp_db)
        binding = {
            "provider": "remote-stt",
            "url": "https://audio.example/v1",
            "model": "whisper-large-v3",
            "api_key": "$secret:REMOTE_STT_API_KEY",
        }
        store.set("voice.openai_compatible_audio", [binding])

        safe_export = client.post("/api/config/export")

        assert safe_export.status_code == 200
        assert safe_export.json()["config_store"]["voice.openai_compatible_audio"] == [binding]

        legacy_binding = {**binding, "api_key": "legacy-plaintext"}
        store.set("voice.openai_compatible_audio", [legacy_binding])

        defensive_export = client.post("/api/config/export")

        assert defensive_export.status_code == 200
        exported_binding = defensive_export.json()["config_store"]["voice.openai_compatible_audio"][
            0
        ]
        assert exported_binding["api_key"] == MASKED_SECRET
        assert exported_binding["model"] == "whisper-large-v3"
        assert "legacy-plaintext" not in defensive_export.text

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
        assert "global/expansion/system.md" in data["prompts"]
        assert "# Custom" in data["prompts"]["global/expansion/system.md"]

    def test_export_config_quotes_prompt_frontmatter_description(
        self,
        client: TestClient,
        temp_db: Any,
        mock_machine_id: Any,
    ) -> None:
        manager = LocalPromptManager(temp_db)
        manager.create_prompt(
            name="test/quoted",
            content="# Body",
            description="alpha: beta",
            scope="global",
        )

        response = client.post("/api/config/export")

        assert response.status_code == 200
        content = response.json()["prompts"]["global/test/quoted.md"]
        frontmatter = yaml.safe_load(content.split("---", maxsplit=2)[1])
        assert frontmatter["description"] == "alpha: beta"

    def test_export_config_keeps_prompt_scope_keys_distinct(
        self,
        temp_db: Any,
        real_config: Any,
        task_manager: Any,
        sample_project: dict[str, Any],
    ) -> None:
        project_id = sample_project["id"]
        manager = LocalPromptManager(temp_db)
        manager.create_prompt(
            name="test/shared",
            content="# Global",
            scope="global",
        )
        manager.create_prompt(
            name="test/shared",
            content="# Project",
            scope="project",
            project_id=project_id,
        )
        server = create_http_server(
            config=real_config,
            database=temp_db,
            task_manager=task_manager,
            project_id=project_id,
        )

        response = TestClient(server.app).post("/api/config/export")

        assert response.status_code == 200
        prompts = response.json()["prompts"]
        assert prompts["global/test/shared.md"] == "# Global"
        assert prompts[f"project/{project_id}/test/shared.md"] == "# Project"

    def test_prompt_export_key_rejects_project_prompt_without_project_id(self) -> None:
        record: SimpleNamespace = SimpleNamespace(
            name="test/missing-project",
            scope="project",
            project_id=None,
        )

        with pytest.raises(ValueError, match="test/missing-project"):
            _prompt_export_key(record)

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

    @pytest.mark.parametrize(
        "payload",
        [
            {
                "config_store": {
                    "daemon_port": 9999,
                    "voice.openai_compatible_audio": [
                        {
                            "provider": "remote-stt",
                            "url": "https://audio.example/v1",
                            "model": "whisper-large-v3",
                            "api_key": "plaintext-key",
                        }
                    ],
                }
            },
            {
                "config": {
                    "daemon_port": 9999,
                    "voice": {
                        "openai_compatible_audio": [
                            {
                                "provider": "remote-stt",
                                "url": "https://audio.example/v1",
                                "model": "whisper-large-v3",
                                "api_key": "plaintext-key",
                            }
                        ]
                    },
                }
            },
        ],
        ids=["config-store", "nested-config"],
    )
    def test_import_rejects_voice_audio_plaintext_before_replacing_config(
        self,
        client: TestClient,
        temp_db: Any,
        payload: dict[str, Any],
    ) -> None:
        store = ConfigStore(temp_db)
        store.set("daemon_port", 5555)

        response = client.post("/api/config/import", json=payload)

        assert response.status_code == 422
        assert "$secret:NAME" in response.json()["detail"]
        assert store.get("daemon_port") == 5555
        assert store.get("voice.openai_compatible_audio") is None

    def test_import_empty_config_store_clears_existing_keys(
        self, client: TestClient, temp_db: Any
    ) -> None:
        store = ConfigStore(temp_db)
        store.set("daemon_port", 5555)

        response = client.post("/api/config/import", json={"config_store": {}})

        assert response.status_code == 200
        assert response.json()["summary"] == "config restored (0 keys)"
        assert store.get("daemon_port") is None

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

    def test_import_legacy_config_embedding_runtime_shape_persists_canonical(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.post(
            "/api/config/import",
            json={"config": {"embeddings": {"model": "bge-m3", "dim": 1024}}},
        )

        assert response.status_code == 200
        store = ConfigStore(temp_db)
        assert store.get(AI_EMBEDDING_MODEL_KEY) == "bge-m3"
        assert store.get("ai.embeddings.dim") == 1024
        assert store.get("embeddings.model") is None

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

    def test_import_frontmatter_only_prompt_uses_empty_body(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.post(
            "/api/config/import",
            json={
                "prompts": {
                    "frontmatter-only.md": "---\ndescription: metadata only\n---\n",
                },
            },
        )

        assert response.status_code == 200
        override = LocalPromptManager(temp_db).get_override("frontmatter-only")
        assert override is not None
        assert override.content == ""
        assert override.description == "metadata only"

    def test_import_prompts_uses_project_scope(
        self,
        temp_db: Any,
        real_config: Any,
        task_manager: Any,
        sample_project: dict[str, Any],
    ) -> None:
        project_id = sample_project["id"]
        server = create_http_server(
            config=real_config,
            database=temp_db,
            task_manager=task_manager,
            project_id=project_id,
        )
        scoped_client = TestClient(server.app)

        response = scoped_client.post(
            "/api/config/import",
            json={"prompts": {"test/imported.md": "# Imported"}},
        )

        assert response.status_code == 200
        override = LocalPromptManager(temp_db).get_override("test/imported", project_id=project_id)
        assert override is not None
        assert override.scope == "project"
        assert override.project_id == project_id

    def test_import_prompts_preserves_exported_scope_and_project_id(
        self,
        client: TestClient,
        temp_db: Any,
    ) -> None:
        # Prompt-override project ids target the native-uuid projects.id
        # column, so the exported path segment must be a valid UUID string.
        project_id = "11111111-1111-4111-8111-111111111111"

        response = client.post(
            "/api/config/import",
            json={
                "prompts": {
                    "global/test/scoped.md": "# Global",
                    f"project/{project_id}/test/scoped.md": "# Project",
                }
            },
        )

        assert response.status_code == 200
        manager = LocalPromptManager(temp_db)
        global_override = manager.get_override("test/scoped")
        project_override = manager.get_override("test/scoped", project_id=project_id)
        assert global_override is not None
        assert global_override.scope == "global"
        assert global_override.content == "# Global"
        assert project_override is not None
        assert project_override.scope == "project"
        assert project_override.project_id == project_id
        assert project_override.content == "# Project"

    def test_import_validates_prompts_before_config_writes(
        self, client: TestClient, temp_db: Any
    ) -> None:
        store = ConfigStore(temp_db)
        store.set("daemon_port", 5555)

        response = client.post(
            "/api/config/import",
            json={
                "config_store": {"daemon_port": 9999},
                "prompts": {
                    "test/bad.md": "---\nvariables: [bad]\n---\n# Bad",
                },
            },
        )

        assert response.status_code == 422
        assert store.get("daemon_port") == 5555

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
        assert response.json()["detail"] == "Failed to persist imported configuration"

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

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "Secret 'service.provider_api_key' must be a string, got dict"
        )
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

    def test_import_config_store_falkordb_password_encrypts(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config_store": {
                    FALKOR_PASSWORD_KEY: "Valid-123",
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
        assert store.get(FALKOR_PASSWORD_KEY) == "$secret:falkordb_password"
        assert store.get("databases.falkordb.rrf_k") == 77
        assert SecretStore(postgres_db).get("falkordb_password") == "Valid-123"

    def test_import_config_store_invalid_falkordb_password_raises_http_exception(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config_store": {
                    "daemon_port": 9999,
                    FALKOR_PASSWORD_KEY: "contains space",
                }
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "FalkorDB password must not contain whitespace"
        store = ConfigStore(postgres_db)
        assert store.get("daemon_port") is None
        assert store.get(FALKOR_PASSWORD_KEY) is None

    def test_import_config_store_non_string_falkordb_password_rejected(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config_store": {
                    FALKOR_PASSWORD_KEY: {"nested": "bad"},
                }
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "Secret 'databases.falkordb.password' must be a string, got dict"
        )
        assert ConfigStore(postgres_db).get(FALKOR_PASSWORD_KEY) is None

    def test_import_config_falkordb_password_encrypts(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config": {
                    "databases": {
                        "falkordb": {
                            "password": "Valid-123",
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
        assert store.get(FALKOR_PASSWORD_KEY) == "$secret:falkordb_password"
        assert store.get("databases.falkordb.rrf_k") == 77
        assert SecretStore(postgres_db).get("falkordb_password") == "Valid-123"

    def test_import_legacy_config_falkordb_requirepass_rejected(
        self, postgres_client: TestClient, postgres_db: Any
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

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid imported configuration"
        store = ConfigStore(postgres_db)
        assert store.get(FALKOR_PASSWORD_KEY) is None
        assert store.get("databases.falkordb.rrf_k") is None

    def test_import_config_non_string_falkordb_password_rejected(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={
                "config": {
                    "databases": {
                        "falkordb": {
                            "password": {"nested": "bad"},
                        }
                    }
                }
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "FalkorDB password must be a string"
        assert ConfigStore(postgres_db).get(FALKOR_PASSWORD_KEY) is None

    def test_import_config_non_object_databases_rejected(
        self, postgres_client: TestClient, postgres_db: Any
    ) -> None:
        response = postgres_client.post(
            "/api/config/import",
            json={"config": {"databases": "not-an-object"}},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "databases must be an object"
        assert ConfigStore(postgres_db).get(FALKOR_PASSWORD_KEY) is None

    def test_import_falkordb_secret_reference_preserves_secret_row(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", SecretStore(postgres_db))
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
                        FALKOR_PASSWORD_KEY: "$secret:falkordb_password",
                        "daemon_port": 9999,
                    },
                    "config_secret_keys": [FALKOR_PASSWORD_KEY],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["requires_restart"] is True
        assert FALKOR_RESTART_HINT_FRAGMENT in data["restart_hint"]
        validator.assert_not_called()
        assert _secret_row(postgres_db) == before_secret
        assert store.get(FALKOR_PASSWORD_KEY) == "$secret:falkordb_password"
        assert FALKOR_PASSWORD_KEY in store.get_secret_keys()
        assert store.get("daemon_port") == 9999

    def test_export_then_import_preserves_password_secret_row(
        self, postgres_client: TestClient, postgres_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(postgres_db)
        store.set_secret(FALKOR_PASSWORD_KEY, "Valid-123", SecretStore(postgres_db))
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
        assert store.get(FALKOR_PASSWORD_KEY) == "$secret:falkordb_password"


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

    def test_get_values_omits_removed_auth_secrets(self, client: TestClient) -> None:
        response = client.get("/api/config/values")
        data = response.json()
        assert "password" not in data["values"]["auth"]
        assert "session_secret" not in data["values"]["auth"]
        assert "auth.password" not in data["secret_keys"]

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

    def test_put_embedding_api_key_uses_canonical_secret(self) -> None:
        class FakeTransaction:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> bool:
                return False

        class FakeStore:
            def __init__(self) -> None:
                self.db = self
                self.entries: dict[str, Any] = {}
                self.secret_keys: set[str] = set()
                self.secrets: dict[str, str] = {}

            def transaction(self) -> FakeTransaction:
                return FakeTransaction()

            def get_secret_keys(self) -> list[str]:
                return sorted(self.secret_keys)

            def set_many(self, entries: dict[str, Any], source: str = "user") -> int:
                self.entries.update(entries)
                return len(entries)

            def set_secret(
                self,
                key: str,
                plaintext_value: str,
                _secret_store: object,
                source: str = "user",
            ) -> None:
                self.entries[key] = "$secret:embeddings_api_key"
                self.secret_keys.add(key)
                self.secrets[EMBEDDING_API_KEY_SECRET_NAME] = plaintext_value

        class FakeContext:
            def __init__(self) -> None:
                self.store = FakeStore()
                self.runtime_config = DaemonConfig()

            def get_config_store(self) -> FakeStore:
                return self.store

            def get_secret_store(self) -> object:
                return object()

            def current_config_values(self) -> dict[str, Any]:
                return self.runtime_config.model_dump(mode="json", exclude_none=True)

            def set_runtime_config(
                self, config: DaemonConfig, *, propagate_websocket: bool = False
            ) -> None:
                self.runtime_config = config

        context = FakeContext()
        app = FastAPI()
        router = APIRouter(prefix="/api/config")
        register_value_routes(router, context)  # type: ignore[arg-type]
        app.include_router(router)
        client = TestClient(app)

        response = client.put(
            "/api/config/values",
            json={"values": {"embeddings": {"api_key": "sk-embed-123"}}},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

        assert context.store.entries[AI_EMBEDDING_API_KEY_KEY] == "$secret:embeddings_api_key"
        assert AI_EMBEDDING_API_KEY_KEY in context.store.secret_keys
        assert context.store.secrets[EMBEDDING_API_KEY_SECRET_NAME] == "sk-embed-123"
        assert context.runtime_config.embeddings.api_key == "sk-embed-123"

    def test_put_existing_embedding_storage_secret_keeps_secret_storage(
        self, client: TestClient, temp_db: Any, mock_machine_id: Any
    ) -> None:
        store = ConfigStore(temp_db)
        secret_store = SecretStore(temp_db)
        store.set_secret(AI_EMBEDDING_MODEL_KEY, "old-secret-model", secret_store)

        response = client.put(
            "/api/config/values",
            json={"values": {"embeddings": {"model": "new-secret-model"}}},
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert store.get(AI_EMBEDDING_MODEL_KEY) == "$secret:model"
        assert AI_EMBEDDING_MODEL_KEY in store.get_secret_keys()
        assert secret_store.get("model") == "new-secret-model"

    def test_put_non_string_embedding_secret_reports_runtime_key(
        self, client: TestClient, temp_db: Any
    ) -> None:
        response = client.put(
            "/api/config/values",
            json={"values": {"embeddings": {"api_key": ["bad"]}}},
        )

        assert response.status_code == 400
        assert "Secret 'embeddings.api_key' must be a string" in response.json()["detail"]
        assert ConfigStore(temp_db).get(AI_EMBEDDING_API_KEY_KEY) is None

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
            json={"values": {"databases": {"falkordb": {"password": "my-secret"}}}},
        )

        # GET should show masked value
        response = client.get("/api/config/values")
        data = response.json()
        assert data["values"]["databases"]["falkordb"]["password"] == "********"

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
                "planPendingVariant": "info",
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
        assert data["planPendingVariant"] == "info"
        assert data["selectedProvider"] == "codex"

    def test_put_and_get_voice_toggles(self, client: TestClient) -> None:
        """STT/TTS/voice-input toggles round-trip (previously dropped server-side)."""
        put_resp = client.put(
            "/api/config/ui-settings",
            json={
                "sttEnabled": False,
                "ttsEnabled": True,
                "voiceInputMode": "vad",
            },
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["ok"] is True

        data = client.get("/api/config/ui-settings").json()
        assert data["sttEnabled"] is False
        assert data["ttsEnabled"] is True
        assert data["voiceInputMode"] == "vad"

    def test_delete_voice_toggle(self, client: TestClient) -> None:
        """A voice toggle is a known key and is deletable like other UI settings."""
        client.put("/api/config/ui-settings", json={"voiceInputMode": "ptt"})

        resp = client.delete("/api/config/ui-settings/voiceInputMode")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "voiceInputMode" not in client.get("/api/config/ui-settings").json()

    def test_put_partial_update(self, client: TestClient) -> None:
        """PUT with only some keys updates only those."""
        client.put("/api/config/ui-settings", json={"fontSize": 14, "theme": "dark"})
        client.put("/api/config/ui-settings", json={"fontSize": 20})

        data = client.get("/api/config/ui-settings").json()
        assert data["fontSize"] == 20
        assert data["theme"] == "dark"

    def test_put_empty_body(self, client: TestClient) -> None:
        """PUT with no fields is rejected."""
        response = client.put("/api/config/ui-settings", json={})
        assert response.status_code == 422

    def test_put_all_null_body(self, client: TestClient) -> None:
        """PUT with only null values is rejected."""
        response = client.put("/api/config/ui-settings", json={"fontSize": None})
        assert response.status_code == 422

    def test_get_backend_error_returns_500(self, client: TestClient) -> None:
        with patch.object(ConfigStore, "get_all", side_effect=RuntimeError("db down")):
            response = client.get("/api/config/ui-settings")

        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"

    def test_put_backend_error_returns_500(self, client: TestClient) -> None:
        with patch.object(ConfigStore, "set_many", side_effect=RuntimeError("db down")):
            response = client.put("/api/config/ui-settings", json={"fontSize": 18})

        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"

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


class TestUISettingsVoiceFields:
    """DB-free contract for the STT/TTS/voice-input UI settings.

    These previously round-tripped through useSettings but were dropped because
    SaveUISettingsRequest omitted them and the route allowlist excluded them.
    Verified without a database so the contract runs even where the route's
    Postgres fixture is unavailable.
    """

    @pytest.mark.parametrize("field", ["sttEnabled", "ttsEnabled", "voiceInputMode"])
    def test_model_declares_voice_field(self, field: str) -> None:
        """The request model declares each voice field so it is not stripped."""
        assert field in SaveUISettingsRequest.model_fields
        assert field in SaveUISettingsRequest.UI_SETTING_FIELDS

    def test_model_accepts_only_a_voice_toggle(self) -> None:
        """A payload with only a voice toggle is a valid (non-empty) update."""
        parsed = SaveUISettingsRequest(sttEnabled=False)
        assert parsed.sttEnabled is False
        assert parsed.ttsEnabled is None
        assert parsed.voiceInputMode is None

    def test_voice_input_mode_round_trips_value(self) -> None:
        parsed = SaveUISettingsRequest(voiceInputMode="vad")
        assert parsed.voiceInputMode == "vad"

    @pytest.mark.parametrize("key", ["sttEnabled", "ttsEnabled", "voiceInputMode"])
    def test_route_allowlist_includes_voice_field(self, key: str) -> None:
        """The route get/save/delete allowlist persists each voice field."""
        assert key in UI_SETTINGS_KEYS
