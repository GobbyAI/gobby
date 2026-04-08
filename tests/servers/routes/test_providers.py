"""Tests for provider availability API routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.providers import create_providers_router

pytestmark = pytest.mark.unit


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    router = create_providers_router()
    app.include_router(router)
    return TestClient(app)


class TestProviderRoutes:
    """Tests for GET /api/providers."""

    def test_list_providers_returns_all_three(self, client: TestClient) -> None:
        """Endpoint returns claude, gemini, and codex entries."""
        response = client.get("/api/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        names = [p["name"] for p in data["providers"]]
        assert names == ["claude", "gemini", "codex"]

    def test_provider_available_when_binary_found(self, client: TestClient) -> None:
        """Provider is marked available when shutil.which finds the binary."""
        with patch("gobby.servers.routes.providers.shutil.which") as mock_which:
            mock_which.side_effect = lambda b: "/usr/bin/claude" if b == "claude" else None
            response = client.get("/api/providers")
            data = response.json()
            providers = {p["name"]: p for p in data["providers"]}
            assert providers["claude"]["available"] is True
            assert providers["claude"]["path"] == "/usr/bin/claude"
            assert providers["gemini"]["available"] is False
            assert providers["gemini"]["path"] is None
            assert providers["codex"]["available"] is False
            assert providers["codex"]["path"] is None

    def test_all_providers_unavailable(self, client: TestClient) -> None:
        """All providers unavailable when no binaries found."""
        with patch("gobby.servers.routes.providers.shutil.which", return_value=None):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is False
                assert p["path"] is None

    def test_all_providers_available(self, client: TestClient) -> None:
        """All providers available when all binaries found."""
        paths = {
            "claude": "/usr/local/bin/claude",
            "gemini": "/usr/local/bin/gemini",
            "codex": "/usr/local/bin/codex",
        }
        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: paths.get(b),
        ):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is True
                assert p["path"] == paths[p["name"]]


class TestProviderModelsRoute:
    """Tests for GET /api/providers/models."""

    def test_returns_all_providers_with_models(self, client: TestClient) -> None:
        """Endpoint returns claude, gemini, and codex with model lists."""
        response = client.get("/api/providers/models")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        providers = {p["provider"]: p for p in data["providers"]}
        assert set(providers.keys()) == {"claude", "gemini", "codex"}

        # Claude should have opus, sonnet, haiku
        claude_values = [m["value"] for m in providers["claude"]["models"]]
        assert claude_values == ["opus", "sonnet", "haiku"]

        # Gemini should have pro, flash
        gemini_values = [m["value"] for m in providers["gemini"]["models"]]
        assert gemini_values == ["pro", "flash"]

        # Each entry should have source field
        for p in data["providers"]:
            assert p["source"] == "static"

    def test_availability_reflects_binary_presence(self, client: TestClient) -> None:
        """Provider availability matches shutil.which results."""
        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: "/bin/claude" if b == "claude" else None,
        ):
            response = client.get("/api/providers/models")
            providers = {p["provider"]: p for p in response.json()["providers"]}
            assert providers["claude"]["available"] is True
            assert providers["gemini"]["available"] is False
            assert providers["codex"]["available"] is False
