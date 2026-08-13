"""Tests for configuration routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/configuration.py endpoints using
create_http_server() with a real DaemonConfig and real SecretStore
backed by a real temp_db.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime
from gobby.config.runtime_models import ConfigSnapshot
from gobby.identity import hash_password
from gobby.prompts.sync import sync_bundled_prompts
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.configuration_prompts import _normalize_variable_spec
from gobby.servers.tool_approvals import DEFAULT_GLOBAL_APPROVAL_RULES
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.config_mutations import (
    ConfigMutations,
    ConfigPatch,
    SecretUpdate,
    config_key_to_secret_name,
)
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.prompts import LocalPromptManager
from gobby.storage.secrets import SecretStore
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.users import LocalUserManager
from tests.fixtures.postgres import TEST_USER_EMAIL, TEST_USER_ID
from tests.servers.conftest import StubConfigRuntime, create_http_server

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
    AuthStore(temp_db).set_local_api_token_hash(hash_token(LOCAL_RUNTIME_TOKEN))
    http_server = create_http_server(
        config=real_config,
        database=temp_db,
        task_manager=task_manager,
    )
    http_server.services.config_runtime = StubConfigRuntime(
        ConfigSnapshot(
            revision=1,
            desired=real_config,
            active=real_config,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={},
        ),
        # These routes exercise the not-ready degrade path of the runtime guards.
        ready=False,
    )
    http_server.auth_service = AuthService(
        lambda: temp_db,
        token_file=tmp_path / "local_cli_token",
    )
    return http_server


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(
        server.app,
        headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
    )


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    (
        ("GET", "/api/config/template", None),
        ("GET", "/api/config/ui-settings", None),
        ("GET", "/api/config/tool-approvals/global", None),
        ("POST", "/api/config/validation-detection/preview", {"command": "pytest"}),
    ),
)
def test_config_reads_return_retryable_503_during_startup(
    server: Any,
    client: TestClient,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    runtime = MagicMock(spec=ConfigRuntime)
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("ConfigRuntime has not started"))
    server.services.config_runtime = runtime

    response = client.request(method, path, json=payload)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    assert response.json()["error"]["retryable"] is True


class TestUISettingsRoundTrip:
    def test_specialized_ui_setting_writers_are_removed(self, client: TestClient) -> None:
        assert client.put("/api/config/ui-settings", json={"fontSize": 18}).status_code == 405
        assert client.delete("/api/config/ui-settings/fontSize").status_code == 404


class TestGlobalToolApprovalRules:
    def test_get_global_tool_approval_rules_defaults(self, client: TestClient) -> None:
        response = client.get("/api/config/tool-approvals/global")
        assert response.status_code == 200
        data = response.json()
        assert data["rules"] == list(DEFAULT_GLOBAL_APPROVAL_RULES)
        assert data["default_rules"] == list(DEFAULT_GLOBAL_APPROVAL_RULES)
        assert "mcp:gobby*:*" in data["built_in_exemptions"]

    def test_specialized_global_tool_approval_writer_is_removed(self, client: TestClient) -> None:
        response = client.put(
            "/api/config/tool-approvals/global",
            json={
                "rules": [" tool:Write ", "tool:Write", "", "mcp:third-party:*"],
            },
        )
        assert response.status_code == 405


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
        assert server.startup_config.bind_host == "localhost"
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
        LocalUserManager(temp_db).update_password(
            TEST_USER_ID,
            hash_password("correct horse battery staple"),
        )
        server.auth_service = AuthService(
            lambda: temp_db,
            token_file=tmp_path / "configured_auth_token",
        )
        browser = TestClient(server.app)

        login_response = browser.post(
            "/api/auth/login",
            json={"email": TEST_USER_EMAIL, "password": "correct horse battery staple"},
        )
        create_response = browser.post(
            "/api/config/secrets",
            json={"name": "SESSION_PROTECTED", "value": "secret"},
        )
        delete_response = browser.delete("/api/config/secrets/SESSION_PROTECTED")

        assert login_response.status_code == 200
        assert create_response.status_code == 200
        assert delete_response.status_code == 200

    def test_non_loopback_bind_requires_authentication(
        self, temp_db: Any, task_manager: Any, tmp_path: Any
    ) -> None:
        http_server = create_http_server(
            config=DaemonConfig(bind_host="0.0.0.0"),
            database=temp_db,
            task_manager=task_manager,
        )
        http_server.auth_service = AuthService(
            lambda: temp_db,
            token_file=tmp_path / "non_loopback_token",
        )
        assert http_server.startup_config is not None
        assert http_server.startup_config.bind_host == "0.0.0.0"

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

    def test_bound_secret_update_validates_new_plaintext_without_leaking_it(
        self,
        client: TestClient,
        server: Any,
        temp_db: HubDatabase,
        mock_machine_id: Any,
    ) -> None:
        key = "databases.falkordb.password"
        name = "bound_falkordb_password"
        previous = "Valid-Password-123"
        invalid = "plaintext must not leak"
        secret_store = SecretStore(temp_db)
        repository = ConfigRepository(temp_db, secret_store=secret_store)
        ConfigMutations(temp_db, secret_store=secret_store).patch(
            expected_revision=repository.current_revision(),
            patch=ConfigPatch(
                secrets={
                    key: SecretUpdate(
                        previous,
                        name=name,
                        category="general",
                    )
                }
            ),
        )
        snapshot = server.services.config_runtime.snapshot
        server.services.config_runtime.current = ConfigSnapshot(
            revision=repository.current_revision(),
            desired=snapshot.desired,
            active=snapshot.active,
            row_revisions=snapshot.row_revisions,
            pending_restart_keys=snapshot.pending_restart_keys,
            failed_live_keys=snapshot.failed_live_keys,
            desired_values=snapshot.desired_values,
            active_values=snapshot.active_values,
        )

        response = client.post(
            "/api/config/secrets",
            json={"name": name, "value": invalid, "category": "general"},
        )

        assert response.status_code == 400
        assert invalid not in response.text
        assert secret_store.get(name) == previous

        patch_response = client.patch(
            "/api/config/values",
            json={
                "expected_revision": repository.current_revision(),
                "values": {"databases": {"falkordb": {"password": invalid}}},
            },
        )
        assert patch_response.status_code == 422
        assert patch_response.json()["error"]["message"] == (
            "Secret configuration value is invalid"
        )
        assert invalid not in patch_response.text

    def test_unbound_canonical_secret_is_validated(
        self,
        client: TestClient,
        temp_db: HubDatabase,
        mock_machine_id: Any,
    ) -> None:
        name = config_key_to_secret_name("databases.falkordb.password")
        invalid = "plaintext must not leak"

        response = client.post(
            "/api/config/secrets",
            json={"name": name, "value": invalid, "category": "general"},
        )

        assert response.status_code == 400
        assert invalid not in response.text
        assert SecretStore(temp_db).get(name) is None

    def test_values_reject_literal_secret_reference(
        self,
        client: TestClient,
        temp_db: HubDatabase,
    ) -> None:
        repository = ConfigRepository(temp_db)

        response = client.patch(
            "/api/config/values",
            json={
                "expected_revision": repository.current_revision(),
                "values": {"databases": {"qdrant": {"api_key": "$secret:external"}}},
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["message"] == (
            "Secret references are not accepted as plaintext values"
        )
        assert SecretStore(temp_db).get("external") is None

    def test_values_plaintext_secret_uses_general_category(
        self,
        client: TestClient,
        temp_db: HubDatabase,
    ) -> None:
        key = "databases.qdrant.api_key"
        repository = ConfigRepository(temp_db)

        response = client.patch(
            "/api/config/values",
            json={
                "expected_revision": repository.current_revision(),
                "values": {"databases": {"qdrant": {"api_key": "qdrant-secret"}}},
            },
        )

        assert response.status_code == 200
        name = config_key_to_secret_name(key)
        info = next(item for item in SecretStore(temp_db).list() if item.name == name)
        assert info.category == "general"

    def test_case_mismatched_referenced_secret_delete_is_refused(
        self,
        client: TestClient,
        temp_db: HubDatabase,
        mock_machine_id: Any,
    ) -> None:
        secret_store = SecretStore(temp_db)
        name = "Case_Referenced_Secret"
        repository = ConfigRepository(temp_db, secret_store=secret_store)
        ConfigMutations(temp_db, secret_store=secret_store).patch(
            expected_revision=repository.current_revision(),
            patch=ConfigPatch(
                values={
                    "ai.generation.endpoints.alpha.api_base": "https://alpha.example/v1",
                    "ai.generation.endpoints.alpha.model": "model-a",
                },
                secrets={
                    "ai.generation.endpoints.alpha.api_key": SecretUpdate(
                        "shared-secret",
                        name=name,
                    )
                },
            ),
        )

        response = client.delete(f"/api/config/secrets/{name.upper()}")

        assert response.status_code == 409
        assert "still referenced" in response.json()["detail"]
        assert secret_store.get(name) == "shared-secret"

    def test_secret_routes_accept_hub_database_protocol(
        self, non_local_hub_db: Any, real_config: Any, tmp_path: Any, mock_machine_id: Any
    ) -> None:
        AuthStore(non_local_hub_db).set_local_api_token_hash(
            hash_token(LOCAL_RUNTIME_TOKEN),
        )
        server = create_http_server(
            config=real_config,
            database=non_local_hub_db,
            task_manager=LocalTaskManager(non_local_hub_db),
        )
        server.auth_service = AuthService(
            lambda: non_local_hub_db,
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
        server.auth_service = MagicMock(spec=AuthService)
        server.auth_service.verify_bearer.return_value = True
        c = TestClient(
            server.app,
            headers={"Authorization": "Bearer test-token"},
        )
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
        AuthStore(temp_db).set_local_api_token_hash(hash_token(LOCAL_RUNTIME_TOKEN))
        scoped_client = TestClient(
            server.app,
            headers={"Authorization": f"Bearer {LOCAL_RUNTIME_TOKEN}"},
        )

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

    def test_get_reads_runtime_snapshot(self, server: Any, client: TestClient) -> None:
        runtime = server.services.config_runtime
        assert isinstance(runtime, StubConfigRuntime)
        snapshot = runtime.snapshot
        runtime.current = ConfigSnapshot(
            revision=snapshot.revision,
            desired=snapshot.desired,
            active=snapshot.active,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={
                "ui_settings.fontSize": 18,
                "ui_settings.voiceInputMode": "vad",
            },
        )

        assert client.get("/api/config/ui-settings").json() == {
            "fontSize": 18,
            "voiceInputMode": "vad",
        }
