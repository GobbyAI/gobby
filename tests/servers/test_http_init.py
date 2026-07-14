"""HTTP server initialization and factory tests."""

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer, create_server
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestHTTPServerInit:
    """Tests for HTTPServer initialization."""

    def test_init_passes_configured_embedding_dim_to_semantic_search(self) -> None:
        """SemanticToolSearch should receive the configured embedding dimension."""
        from gobby.config.persistence import EmbeddingsConfig

        mock_config = MagicMock()
        mock_config.embeddings = EmbeddingsConfig(model="test-model", dim=1024, api_base=None)
        mock_config.websocket = None

        services = ServiceContainer(
            config=mock_config,
            database=MagicMock(),
            session_manager=None,
            task_manager=MagicMock(),
            llm_service=MagicMock(),
            mcp_manager=MagicMock(),
            mcp_db_manager=MagicMock(db=MagicMock()),
        )

        with (
            patch("gobby.storage.secrets.SecretStore") as mock_secret_store,
            patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
            patch("gobby.storage.merge_resolutions.MergeResolutionManager"),
            patch("gobby.worktrees.merge.resolver.MergeResolver"),
            patch("gobby.servers.http.setup_internal_registries", return_value=[]),
            patch("gobby.servers.http.SemanticToolSearch") as mock_semantic_search,
            patch("gobby.servers.http.GobbyDaemonTools"),
            patch("gobby.servers.http.create_mcp_server"),
            patch("gobby.servers.app_factory.create_app", return_value=FastAPI()),
        ):
            mock_secret_store.return_value.get.return_value = None
            HTTPServer(services=services, port=8000, test_mode=True)

        assert mock_semantic_search.call_args is not None
        assert mock_semantic_search.call_args.kwargs["embedding_dim"] == 1024

    def test_init_minimal(self) -> None:
        """Test HTTPServer with minimal configuration."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert server.port == 8000
        assert server.test_mode is True
        assert server.mcp_manager is None
        assert server.config is services.config
        assert server.session_manager is services.session_manager
        assert server._mcp_server is None
        assert server._internal_manager is None
        assert server._tools_handler is None

    def test_init_with_port(self) -> None:
        """Test HTTPServer with custom port."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=9999, test_mode=False)
        assert server.port == 9999
        assert server.test_mode is False

    def test_init_sets_start_time(self) -> None:
        """Test that HTTPServer sets start time."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        before = time.time()
        server = HTTPServer(services=services, port=8000, test_mode=True)
        after = time.time()
        assert before <= server._start_time <= after
        assert server.services is services

    def test_init_creates_broadcaster(self) -> None:
        """Test that HTTPServer creates broadcaster."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert server.broadcaster is not None
        assert server.services is services

    def test_init_with_session_manager(self, session_storage: SessionManager) -> None:
        """Test HTTPServer with session manager."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        assert server.session_manager is session_storage

    def test_init_background_tasks_empty(self) -> None:
        """Test that background tasks set is initialized empty."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert isinstance(server._background_tasks, set)
        assert len(server._background_tasks) == 0

    def test_init_running_flag_false(self) -> None:
        """Test that _running is initially False."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert server._running is False
        assert server.services is services

    def test_init_creates_app(self) -> None:
        """Test that HTTPServer creates FastAPI app."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)
        assert isinstance(server.app, FastAPI)
        assert server.services is services

    def test_init_with_llm_service(self) -> None:
        """Test HTTPServer with provided LLM service."""
        mock_llm = MagicMock()
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            llm_service=mock_llm,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        assert server.llm_service is mock_llm
        assert server.services.llm_service is mock_llm

    def test_init_preserves_missing_llm_service(self) -> None:
        """HTTPServer leaves runner-owned LLM and text-generation services missing."""
        mock_config = MagicMock()

        services = ServiceContainer(
            config=mock_config,
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )

        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )

        assert server.llm_service is services.llm_service is None
        assert server.services.text_generation_service is None


class TestResolveProjectId:
    """Tests for resolve_project_id method."""

    def test_resolve_with_explicit_project_id(self, basic_http_server: HTTPServer) -> None:
        """Test that explicit project_id is returned directly."""
        result = basic_http_server.resolve_project_id("explicit-id", None)
        assert result == "explicit-id"

    def test_resolve_from_cwd(
        self, basic_http_server: HTTPServer, temp_dir: Path, test_project: dict[str, Any]
    ) -> None:
        """Test resolving project_id from cwd."""
        result = basic_http_server.resolve_project_id(None, str(temp_dir))
        assert result == test_project["id"]

    def test_resolve_no_project_json_raises(
        self, basic_http_server: HTTPServer, temp_dir: Path
    ) -> None:
        """Test that missing project.json raises ValueError."""
        no_project_dir = temp_dir / "no_project"
        no_project_dir.mkdir()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError) as exc_info:
                basic_http_server.resolve_project_id(None, str(no_project_dir))

        assert "No .gobby/project.json found" in str(exc_info.value)
        assert "gobby init" in str(exc_info.value)

    def test_resolve_with_cwd_default(self, basic_http_server: HTTPServer) -> None:
        """Test resolution uses current directory when cwd is None."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "default-project-id", "name": "test"}

            result = basic_http_server.resolve_project_id(None, None)
            assert result == "default-project-id"


class TestCreateServer:
    """Tests for create_server function."""

    @pytest.mark.asyncio
    async def test_create_server_minimal(self) -> None:
        """Test create_server with minimal arguments."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = await create_server(services=services, port=8000, test_mode=True)

        assert isinstance(server, HTTPServer)
        assert server.port == 8000
        assert server.test_mode is True

    @pytest.mark.asyncio
    async def test_create_server_with_all_args(self, session_storage: SessionManager) -> None:
        """Test create_server with all arguments."""
        mock_mcp_manager = MagicMock()
        mock_config = MagicMock()

        services = ServiceContainer(
            config=mock_config,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )

        server = await create_server(
            services=services,
            port=9000,
            test_mode=False,
        )

        assert server.port == 9000
        assert server.test_mode is False
        assert server.mcp_manager is mock_mcp_manager
        assert server.config is mock_config
        assert server.session_manager is session_storage
