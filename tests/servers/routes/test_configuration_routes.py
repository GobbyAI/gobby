"""Tests for configuration routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/configuration.py endpoints using
create_http_server() with a real DaemonConfig and real SecretStore
backed by a real temp_db.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.prompts.sync import sync_bundled_prompts
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.configuration_models import SaveUISettingsRequest
from gobby.servers.routes.configuration_prompts import _normalize_variable_spec
from gobby.servers.routes.configuration_ui_settings import UI_SETTINGS_KEYS
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
        assert http_server.services.config is not None
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

    def test_list_prompts_total_is_filter_scoped_and_unpaginated(
        self,
        client: TestClient,
        temp_db: HubDatabase,
    ) -> None:
        initial = client.get("/api/config/prompts").json()
        prompt_path = initial["prompts"][0]["path"]

        override = client.put(
            f"/api/config/prompts/{prompt_path}",
            json={"content": "# Override"},
        )
        assert override.status_code == 200
        LocalPromptManager(temp_db).create_prompt(
            name="test/disabled",
            content="disabled",
            scope="global",
            enabled=False,
        )

        data = client.get("/api/config/prompts?limit=1").json()
        assert data["total"] == initial["total"]
        assert data["count"] == 1
        assert len(data["prompts"]) == 1

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
