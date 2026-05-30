"""Tests for skills definition API routes - real coverage, minimal mocking."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def skill_manager() -> MagicMock:
    sm = MagicMock()
    return sm


@pytest.fixture
def hub_manager() -> MagicMock:
    hm = MagicMock()
    return hm


@pytest.fixture
def websocket_server() -> MagicMock:
    ws = MagicMock()
    ws.broadcast_skill_event = AsyncMock()
    return ws


@pytest.fixture
def server(
    skill_manager: MagicMock,
    hub_manager: MagicMock,
    websocket_server: MagicMock,
    temp_db: HubDatabase,
) -> HTTPServer:
    svr = create_http_server(
        config=DaemonConfig(),
        websocket_server=websocket_server,
        database=temp_db,
    )
    # Monkey-patch these managers since they aren't part of ServiceContainer initially
    svr.skill_manager = skill_manager
    svr.hub_manager = hub_manager
    return svr


@pytest.fixture
def client(server: HTTPServer) -> TestClient:
    return TestClient(server.app)


@pytest.fixture
def skill_project(temp_db: HubDatabase, tmp_path: Path) -> Project:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    return LocalProjectManager(temp_db).create(name="skills-project", repo_path=str(repo_path))


class TestListSkills:
    def test_list_skills(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "test-id", "name": "test-skill"}
        skill_manager.list_skills.return_value = [skill_mock]

        response = client.get("/api/skills")

        assert response.status_code == 200
        assert response.json()["skills"][0]["name"] == "test-skill"
        skill_manager.list_skills.assert_called_once()

    def test_list_skills_error(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_manager.list_skills.side_effect = Exception("DB error")
        response = client.get("/api/skills")
        assert response.status_code == 500


class TestCreateSkill:
    def test_create_skill_success(
        self, client: TestClient, skill_manager: MagicMock, websocket_server: MagicMock
    ) -> None:
        skill_mock = MagicMock()
        skill_mock.id = "new-id"
        skill_mock.to_dict.return_value = {"id": "new-id", "name": "new-skill"}
        skill_manager.create_skill.return_value = skill_mock

        payload = {
            "name": "new-skill",
            "description": "test desc",
            "content": "test content",
        }
        response = client.post("/api/skills", json=payload)

        assert response.status_code == 201
        assert response.json()["id"] == "new-id"
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_created", "new-id")

    def test_create_skill_value_error(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_manager.create_skill.side_effect = ValueError("Invalid name")
        payload = {
            "name": "bad_name!",
            "description": "test desc",
            "content": "test content",
        }
        response = client.post("/api/skills", json=payload)
        assert response.status_code == 409

    def test_create_skill_exception(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_manager.create_skill.side_effect = Exception("Fail")
        payload = {
            "name": "new-skill",
            "description": "test desc",
            "content": "test content",
        }
        response = client.post("/api/skills", json=payload)
        assert response.status_code == 500


class TestSearchSkills:
    def test_search_skills(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "id-1", "name": "found-skill"}
        skill_manager.search_skills.return_value = [skill_mock]

        response = client.get("/api/skills/search?q=found")
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["name"] == "found-skill"

    def test_search_skills_error(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_manager.search_skills.side_effect = Exception("Fail")
        response = client.get("/api/skills/search?q=found")
        assert response.status_code == 500


class TestSkillStats:
    def test_skill_stats(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_manager.count_skills.side_effect = [10, 8, 2, 5]

        skill_mock1 = MagicMock()
        skill_mock1.get_category.return_value = "cat1"
        skill_mock1.source_type = "filesystem"
        skill_mock1.hub_name = None

        skill_mock2 = MagicMock()
        skill_mock2.get_category.return_value = None
        skill_mock2.source_type = "hub"
        skill_mock2.hub_name = "official"

        skill_manager.list_skills.return_value = [skill_mock1, skill_mock2]

        response = client.get("/api/skills/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 10
        assert data["enabled"] == 8
        assert data["disabled"] == 2
        assert data["bundled"] == 1
        assert data["from_hubs"] == 1
        assert data["templates"] == 0
        assert data["installed_count"] == 5
        assert data["by_category"]["cat1"] == 1
        assert data["by_category"]["uncategorized"] == 1
        assert data["by_source_type"]["filesystem"] == 1
        assert data["by_source_type"]["hub"] == 1

    def test_skill_stats_error(self, client: TestClient, skill_manager: MagicMock) -> None:
        skill_manager.count_skills.side_effect = Exception("Fail")
        response = client.get("/api/skills/stats")
        assert response.status_code == 500


class TestRestoreDefaults:
    @patch("gobby.skills.sync.sync_bundled_skills")
    def test_restore_defaults(
        self, mock_sync, client: TestClient, websocket_server: MagicMock
    ) -> None:
        mock_sync.return_value = {"sync": "done"}
        response = client.post("/api/skills/restore-defaults")
        assert response.status_code == 200
        assert response.json() == {"sync": "done"}
        websocket_server.broadcast_skill_event.assert_awaited_once_with(
            "skills_bulk_changed", "bulk"
        )

    @patch("gobby.skills.sync.sync_bundled_skills")
    def test_restore_defaults_error(self, mock_sync, client: TestClient) -> None:
        mock_sync.side_effect = Exception("Fail")
        response = client.post("/api/skills/restore-defaults")
        assert response.status_code == 500


class TestImportSkill:
    @patch("gobby.skills.loader.SkillLoader")
    def test_import_github(
        self, MockLoader, client: TestClient, skill_manager, websocket_server
    ) -> None:
        mock_loader = MockLoader.return_value
        parsed_mock = MagicMock()
        parsed_mock.name = "git-skill"
        parsed_mock.description = "des"
        parsed_mock.content = "con"
        parsed_mock.version = "1"
        parsed_mock.license = None
        parsed_mock.compatibility = None
        parsed_mock.allowed_tools = None
        parsed_mock.metadata = None
        parsed_mock.source_path = "p"
        parsed_mock.source_type = "st"
        parsed_mock.source_ref = "ref"
        parsed_mock.always_apply = False
        parsed_mock.injection_format = "format"
        mock_loader.load_from_github.return_value = parsed_mock

        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"name": "git-skill"}
        skill_manager.create_skill.return_value = skill_mock

        response = client.post("/api/skills/import", json={"source": "github:user/repo"})
        assert response.status_code == 200
        assert response.json()["imported"] == 1
        websocket_server.broadcast_skill_event.assert_awaited_once_with(
            "skills_bulk_changed", "bulk"
        )

    @patch("gobby.skills.loader.SkillLoader")
    def test_import_zip(self, MockLoader, client: TestClient, skill_manager, skill_project) -> None:
        mock_loader = MockLoader.return_value
        parsed_mock = MagicMock()
        parsed_mock.name = "zip-skill"
        parsed_mock.source_type = "agent"
        mock_loader.load_from_zip.return_value = [parsed_mock]

        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"name": "zip-skill"}
        skill_manager.create_skill.return_value = skill_mock

        response = client.post(
            "/api/skills/import",
            json={"source": "file.zip", "project_id": skill_project.id},
        )
        assert response.status_code == 200

    @patch("gobby.skills.loader.SkillLoader")
    def test_import_local(
        self, MockLoader, client: TestClient, skill_manager, skill_project
    ) -> None:
        mock_loader = MockLoader.return_value
        parsed_mock = MagicMock()
        parsed_mock.name = "local-skill"
        parsed_mock.source_type = "agent"
        mock_loader.load_skill.return_value = parsed_mock

        skill_manager.create_skill.side_effect = ValueError("duplicate")

        response = client.post(
            "/api/skills/import",
            json={"source": ".", "project_id": skill_project.id},
        )
        assert response.status_code == 200
        assert response.json()["imported"] == 0

    @patch("gobby.skills.loader.SkillLoader")
    def test_import_prefers_existing_project_relative_path_over_github(
        self, MockLoader, client: TestClient, skill_manager, skill_project
    ) -> None:
        local_skill = Path(skill_project.repo_path) / "foo" / "bar"
        local_skill.mkdir(parents=True)
        mock_loader = MockLoader.return_value
        parsed_mock = MagicMock()
        parsed_mock.name = "local-skill"
        parsed_mock.source_type = "agent"
        mock_loader.load_skill.return_value = parsed_mock

        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"name": "local-skill"}
        skill_manager.create_skill.return_value = skill_mock

        response = client.post(
            "/api/skills/import",
            json={"source": "foo/bar", "project_id": skill_project.id},
        )

        assert response.status_code == 200
        mock_loader.load_from_github.assert_not_called()
        mock_loader.load_skill.assert_called_once_with(str(local_skill.resolve()), validate=True)

    @patch("gobby.skills.loader.SkillLoader")
    def test_import_error(self, MockLoader, client: TestClient, skill_project) -> None:
        mock_loader = MockLoader.return_value
        mock_loader.load_skill.side_effect = Exception("Fail")
        response = client.post(
            "/api/skills/import",
            json={"source": ".", "project_id": skill_project.id},
        )
        assert response.status_code == 500

    def test_import_local_requires_project_id(self, client: TestClient) -> None:
        response = client.post("/api/skills/import", json={"source": "file.zip"})
        assert response.status_code == 400

    def test_import_local_rejects_project_escape(self, client: TestClient, skill_project) -> None:
        response = client.post(
            "/api/skills/import",
            json={"source": "../outside.zip", "project_id": skill_project.id},
        )
        assert response.status_code == 403


class TestScanSkill:
    @patch("gobby.skills.scanner.scan_skill_content")
    def test_scan_skill(self, mock_scan, client: TestClient) -> None:
        mock_scan.return_value = {"safe": True}
        response = client.post("/api/skills/scan", json={"content": "safe text", "name": "n"})
        assert response.status_code == 200
        assert response.json() == {"safe": True}

    @patch("gobby.skills.scanner.scan_skill_content")
    def test_scan_skill_missing_package(self, mock_scan, client: TestClient) -> None:
        mock_scan.side_effect = ImportError
        response = client.post("/api/skills/scan", json={"content": "text"})
        assert response.status_code == 501

    @patch("gobby.skills.scanner.scan_skill_content")
    def test_scan_skill_error(self, mock_scan, client: TestClient) -> None:
        mock_scan.side_effect = Exception("Fail")
        response = client.post("/api/skills/scan", json={"content": "text"})
        assert response.status_code == 500


class TestHubs:
    def test_list_hubs_none(self, client: TestClient, server) -> None:
        server.hub_manager = None
        response = client.get("/api/skills/hubs")
        assert response.status_code == 200
        assert response.json()["hubs"] == []

    def test_list_hubs(self, client: TestClient, hub_manager) -> None:
        hub_manager.list_hubs.return_value = ["hub1", "hub2"]
        mock_config1 = MagicMock()
        mock_config1.type = "github"
        mock_config1.base_url = "url"
        mock_config1.repo = "repo"

        def getter(name):
            if name == "hub1":
                return mock_config1
            raise KeyError()

        hub_manager.get_config.side_effect = getter
        response = client.get("/api/skills/hubs")
        assert response.status_code == 200
        assert len(response.json()["hubs"]) == 1
        assert response.json()["hubs"][0]["name"] == "hub1"

    def test_list_hubs_error(self, client: TestClient, hub_manager) -> None:
        hub_manager.list_hubs.side_effect = Exception("Fail")
        response = client.get("/api/skills/hubs")
        assert response.status_code == 500

    def test_search_hubs_none(self, client: TestClient, server) -> None:
        server.hub_manager = None
        response = client.get("/api/skills/hubs/search?q=test")
        assert response.status_code == 200

    def test_search_hubs(self, client: TestClient, hub_manager) -> None:
        hub_manager.search_all = AsyncMock(return_value=([{"name": "h1"}], {}))
        response = client.get("/api/skills/hubs/search?q=test&hub_name=hubbie")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_search_hubs_error(self, client: TestClient, hub_manager) -> None:
        hub_manager.search_all = AsyncMock(side_effect=Exception("Fail"))
        response = client.get("/api/skills/hubs/search?q=test")
        assert response.status_code == 500

    def test_search_hubs_none_includes_empty_hub_errors(self, client: TestClient, server) -> None:
        """No-manager branch now returns a stable shape with an empty hub_errors dict."""
        server.hub_manager = None
        response = client.get("/api/skills/hubs/search?q=test")
        assert response.status_code == 200
        body = response.json()
        assert body["hub_errors"] == {}

    def test_search_hubs_always_includes_hub_errors_on_success(
        self, client: TestClient, hub_manager
    ) -> None:
        """200 responses always carry a hub_errors key even when no hub failed."""
        hub_manager.search_all = AsyncMock(return_value=([{"name": "h1"}], {}))
        response = client.get("/api/skills/hubs/search?q=test")
        assert response.status_code == 200
        body = response.json()
        assert body["hub_errors"] == {}

    def test_search_hubs_surfaces_per_hub_errors(self, client: TestClient, hub_manager) -> None:
        """Per-hub errors from search_all are surfaced in hub_errors."""
        hub_manager.search_all = AsyncMock(
            return_value=([{"name": "h1"}], {"skillsmp": "auth failed"})
        )
        response = client.get("/api/skills/hubs/search?q=test")
        assert response.status_code == 200
        body = response.json()
        assert body["hub_errors"] == {"skillsmp": "auth failed"}
        assert body["count"] == 1

    def test_list_hubs_includes_auth_status(self, client: TestClient, hub_manager) -> None:
        """Each /hubs entry carries auth_required / auth_configured / auth_key_name."""
        hub_manager.list_hubs.return_value = ["open-hub", "authed-hub"]

        open_cfg = MagicMock()
        open_cfg.type = "clawdhub"
        open_cfg.base_url = None
        open_cfg.repo = None

        authed_cfg = MagicMock()
        authed_cfg.type = "skillsmp"
        authed_cfg.base_url = "https://skillsmp.com/api/v1"
        authed_cfg.repo = None

        def get_config(name):
            return {"open-hub": open_cfg, "authed-hub": authed_cfg}[name]

        hub_manager.get_config.side_effect = get_config

        def auth_status(name):
            if name == "open-hub":
                return {"auth_required": False, "auth_configured": True}
            return {
                "auth_required": True,
                "auth_key_name": "SKILLSMP_API_KEY",
                "auth_configured": False,
            }

        hub_manager.auth_status.side_effect = auth_status

        response = client.get("/api/skills/hubs")
        assert response.status_code == 200
        hubs = {h["name"]: h for h in response.json()["hubs"]}
        assert hubs["open-hub"]["auth_required"] is False
        assert hubs["open-hub"]["auth_configured"] is True
        assert hubs["authed-hub"]["auth_required"] is True
        assert hubs["authed-hub"]["auth_key_name"] == "SKILLSMP_API_KEY"
        assert hubs["authed-hub"]["auth_configured"] is False

    def test_list_hubs_falls_back_when_auth_status_runtime_fails(
        self, client: TestClient, hub_manager
    ) -> None:
        """Runtime auth-status failures degrade to an unknown-but-renderable shape."""
        hub_manager.list_hubs.return_value = ["authed-hub"]

        config = MagicMock()
        config.type = "skillsmp"
        config.base_url = "https://skillsmp.com/api/v1"
        config.repo = None
        hub_manager.get_config.return_value = config
        hub_manager.auth_status.side_effect = RuntimeError("secret store unavailable")

        response = client.get("/api/skills/hubs")

        assert response.status_code == 200
        hub = response.json()["hubs"][0]
        assert hub["name"] == "authed-hub"
        assert hub["auth_configured"] is None
        assert hub["auth_key_name"] is None

    def test_list_hubs_surfaces_programmer_errors_from_auth_status(
        self, client: TestClient, hub_manager
    ) -> None:
        """Type/attribute/value errors should bubble instead of degrading silently."""
        hub_manager.list_hubs.return_value = ["authed-hub"]

        config = MagicMock()
        config.type = "skillsmp"
        config.base_url = "https://skillsmp.com/api/v1"
        config.repo = None
        hub_manager.get_config.return_value = config
        hub_manager.auth_status.side_effect = ValueError("bad hub wiring")

        response = client.get("/api/skills/hubs")

        assert response.status_code == 500
        assert response.json()["detail"] == "bad hub wiring"

    @patch("gobby.skills.loader.SkillLoader")
    def test_install_from_hub(
        self, MockLoader, client: TestClient, hub_manager, skill_manager, websocket_server
    ) -> None:
        mock_provider = MagicMock()
        mock_download = MagicMock()
        mock_download.success = True
        mock_download.path = "/tmp/download"
        mock_download.version = "1.0"
        mock_provider.download_skill = AsyncMock(return_value=mock_download)
        hub_manager.get_provider.return_value = mock_provider

        mock_loader = MockLoader.return_value
        parsed_mock = MagicMock()
        parsed_mock.name = "hub-skill"
        parsed_mock.description = "des"
        parsed_mock.content = "con"
        parsed_mock.version = None
        parsed_mock.license = None
        parsed_mock.compatibility = None
        parsed_mock.allowed_tools = None
        parsed_mock.metadata = None
        parsed_mock.always_apply = False
        parsed_mock.injection_format = "format"
        mock_loader.load_skill.return_value = parsed_mock

        skill_mock = MagicMock()
        skill_mock.id = "did"
        skill_mock.to_dict.return_value = {"name": "hub-skill"}
        skill_manager.create_skill.return_value = skill_mock

        response = client.post(
            "/api/skills/hubs/install", json={"hub_name": "hubbi", "slug": "sluggi"}
        )
        assert response.status_code == 200
        assert response.json()["installed"] is True
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_created", "did")

    def test_install_from_hub_none(self, client: TestClient, server) -> None:
        server.hub_manager = None
        response = client.post("/api/skills/hubs/install", json={"hub_name": "h", "slug": "s"})
        assert response.status_code == 404

    def test_install_from_hub_download_fail(self, client: TestClient, hub_manager) -> None:
        mock_provider = MagicMock()
        mock_download = MagicMock()
        mock_download.success = False
        mock_download.error = "Nope"
        mock_provider.download_skill = AsyncMock(return_value=mock_download)
        hub_manager.get_provider.return_value = mock_provider

        response = client.post("/api/skills/hubs/install", json={"hub_name": "h", "slug": "s"})
        assert response.status_code == 502

    @patch("gobby.skills.loader.SkillLoader")
    def test_install_from_hub_conflict(
        self, MockLoader, client: TestClient, hub_manager, skill_manager
    ) -> None:
        mock_provider = MagicMock()
        mock_download = MagicMock()
        mock_download.success = True
        mock_provider.download_skill = AsyncMock(return_value=mock_download)
        hub_manager.get_provider.return_value = mock_provider

        mock_loader = MockLoader.return_value
        parsed_mock = MagicMock()
        mock_loader.load_skill.return_value = parsed_mock

        skill_manager.create_skill.side_effect = ValueError("exists")
        response = client.post("/api/skills/hubs/install", json={"hub_name": "h", "slug": "s"})
        assert response.status_code == 409
        assert "exists" in response.text

    def test_install_from_hub_error(self, client: TestClient, hub_manager) -> None:
        hub_manager.get_provider.side_effect = Exception("Fail")
        response = client.post("/api/skills/hubs/install", json={"hub_name": "h", "slug": "s"})
        assert response.status_code == 500


class TestInstallAllTemplates:
    def test_install_all_templates_returns_410(self, client: TestClient) -> None:
        """Legacy endpoint returns 410 Gone."""
        response = client.post("/api/skills/install-all-templates")
        assert response.status_code == 410


class TestGetSkill:
    def test_get_skill(self, client: TestClient, skill_manager) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "1", "name": "s"}
        skill_manager.get_skill.return_value = skill_mock
        response = client.get("/api/skills/1")
        assert response.status_code == 200

    def test_get_skill_not_found(self, client: TestClient, skill_manager) -> None:
        skill_manager.get_skill.side_effect = ValueError("NF")
        response = client.get("/api/skills/1")
        assert response.status_code == 404

    def test_get_skill_error(self, client: TestClient, skill_manager) -> None:
        skill_manager.get_skill.side_effect = Exception("err")
        response = client.get("/api/skills/1")
        assert response.status_code == 500


class TestUpdateSkill:
    def test_update_skill(self, client: TestClient, skill_manager, websocket_server) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "1", "name": "new"}
        skill_manager.update_skill.return_value = skill_mock
        response = client.put("/api/skills/1", json={"name": "new"})
        assert response.status_code == 200
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_updated", "1")

    def test_update_skill_not_found(self, client: TestClient, skill_manager) -> None:
        skill_manager.update_skill.side_effect = ValueError("NF")
        response = client.put("/api/skills/1", json={"name": "new"})
        assert response.status_code == 404

    def test_update_skill_error(self, client: TestClient, skill_manager) -> None:
        skill_manager.update_skill.side_effect = Exception("err")
        response = client.put("/api/skills/1", json={"name": "new"})
        assert response.status_code == 500


class TestDeleteSkill:
    def test_delete_skill(self, client: TestClient, skill_manager, websocket_server) -> None:
        skill_manager.delete_skill.return_value = True
        response = client.delete("/api/skills/1")
        assert response.status_code == 200
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_deleted", "1")

    def test_delete_skill_not_found(self, client: TestClient, skill_manager) -> None:
        skill_manager.delete_skill.return_value = False
        response = client.delete("/api/skills/1")
        assert response.status_code == 404

    def test_delete_skill_error(self, client: TestClient, skill_manager) -> None:
        skill_manager.delete_skill.side_effect = Exception("err")
        response = client.delete("/api/skills/1")
        assert response.status_code == 500


class TestInstallFromTemplate:
    def test_install_from_template_returns_410(self, client: TestClient) -> None:
        """Legacy endpoint returns 410 Gone."""
        response = client.post("/api/skills/1/install")
        assert response.status_code == 410


class TestMoveToProject:
    def test_move_to_project(self, client: TestClient, skill_manager, websocket_server) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "1"}
        skill_manager.move_to_project.return_value = skill_mock
        response = client.post("/api/skills/1/move-to-project?project_id=2")
        assert response.status_code == 200
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_updated", "1")

    def test_move_to_project_val_err(self, client: TestClient, skill_manager) -> None:
        skill_manager.move_to_project.side_effect = ValueError("E")
        response = client.post("/api/skills/1/move-to-project?project_id=2")
        assert response.status_code == 400

    def test_move_to_project_err(self, client: TestClient, skill_manager) -> None:
        skill_manager.move_to_project.side_effect = Exception("E")
        response = client.post("/api/skills/1/move-to-project?project_id=2")
        assert response.status_code == 500


class TestMoveToInstalled:
    def test_move_to_installed(self, client: TestClient, skill_manager, websocket_server) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "1"}
        skill_manager.move_to_installed.return_value = skill_mock
        response = client.post("/api/skills/1/move-to-installed")
        assert response.status_code == 200
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_updated", "1")

    def test_move_to_installed_val_err(self, client: TestClient, skill_manager) -> None:
        skill_manager.move_to_installed.side_effect = ValueError("E")
        response = client.post("/api/skills/1/move-to-installed")
        assert response.status_code == 400

    def test_move_to_installed_err(self, client: TestClient, skill_manager) -> None:
        skill_manager.move_to_installed.side_effect = Exception("E")
        response = client.post("/api/skills/1/move-to-installed")
        assert response.status_code == 500


class TestRestoreSkill:
    def test_restore_skill(self, client: TestClient, skill_manager, websocket_server) -> None:
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"id": "1"}
        skill_manager.restore_skill.return_value = skill_mock
        response = client.post("/api/skills/1/restore")
        assert response.status_code == 200
        websocket_server.broadcast_skill_event.assert_awaited_once_with("skill_updated", "1")

    def test_restore_skill_not_found(self, client: TestClient, skill_manager) -> None:
        skill_manager.restore_skill.side_effect = ValueError("E")
        response = client.post("/api/skills/1/restore")
        assert response.status_code == 404

    def test_restore_skill_err(self, client: TestClient, skill_manager) -> None:
        skill_manager.restore_skill.side_effect = Exception("E")
        response = client.post("/api/skills/1/restore")
        assert response.status_code == 500


class TestExportSkill:
    def test_export_skill(self, client: TestClient, skill_manager) -> None:
        skill_mock = MagicMock()
        skill_mock.id = "1"
        skill_mock.name = "sn"
        skill_mock.description = "des"
        skill_mock.version = "v1"
        skill_mock.license = "MIT"
        skill_mock.compatibility = "1"
        skill_mock.allowed_tools = ["t"]
        skill_mock.metadata = {"m": "v"}
        skill_mock.content = "content"
        skill_manager.get_skill.return_value = skill_mock

        response = client.get("/api/skills/1/export")
        assert response.status_code == 200
        assert "content" in response.json()["content"]

    def test_export_skill_not_found(self, client: TestClient, skill_manager) -> None:
        skill_manager.get_skill.side_effect = ValueError("NF")
        response = client.get("/api/skills/1/export")
        assert response.status_code == 404

    def test_export_skill_err(self, client: TestClient, skill_manager) -> None:
        skill_manager.get_skill.side_effect = Exception("E")
        response = client.get("/api/skills/1/export")
        assert response.status_code == 500
