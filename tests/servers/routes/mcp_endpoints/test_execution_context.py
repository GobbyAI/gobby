"""Tests for _set_context_for_request in execution.py.

After Change 2b, _set_context_for_request delegates to the shared
resolve_and_seed_contexts helper. These tests verify the HTTP-specific
bootstrap (deriving project_id from the X-Gobby-Session-Id header when
the incoming ref is #N/numeric) and that dispatchers always propagate the
resolved platform UUID.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from gobby.servers.routes.mcp.endpoints.execution import _set_context_for_request
from gobby.utils.session_context import SeededContextTokens

pytestmark = pytest.mark.unit

SESSION_UUID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())


def _make_server(db: MagicMock | None = None) -> MagicMock:
    server = MagicMock()
    server.session_manager = MagicMock()
    if db is not None:
        server.session_manager.db = db
    return server


def _make_request(
    project_id: str | None = None,
    session_id: str | None = None,
) -> MagicMock:
    request = MagicMock()
    headers: dict[str, str] = {}
    if project_id:
        headers["x-gobby-project-id"] = project_id
    if session_id:
        headers["x-gobby-session-id"] = session_id
    request.headers = headers
    return request


class TestSetContextForRequest:
    """Tests for _set_context_for_request after helper extraction."""

    def test_hash_n_ref_forwarded_to_helper_with_header_project_scope(self) -> None:
        """#N reference is forwarded to resolve_and_seed_contexts with the header project scope."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_session_id=SESSION_UUID),
        ) as mock_helper:
            _set_context_for_request(server, {"session_id": "#5"}, request)

        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        assert kwargs["session_ref"] == "#5"
        assert kwargs["project_ref"] == PROJECT_ID
        assert kwargs["project_ref_is_fallback"] is True

    def test_uuid_session_id_forwarded_verbatim_to_helper(self) -> None:
        """UUID-shaped refs are handed to the helper; the resolver resolves external_id → id."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_session_id=SESSION_UUID),
        ) as mock_helper:
            external_uuid = str(uuid.uuid4())
            _set_context_for_request(server, {"session_id": external_uuid}, request)

        # Flip of the old lock-in: UUID-shaped refs no longer bypass resolution.
        assert mock_helper.call_args.kwargs["session_ref"] == external_uuid

    def test_hash_n_no_project_header_bootstraps_from_header_session(self) -> None:
        """#N ref without x-gobby-project-id derives project from x-gobby-session-id."""
        server = _make_server()
        header_session_uuid = str(uuid.uuid4())
        request = _make_request(session_id=header_session_uuid)  # no project_id header

        bootstrap_session = MagicMock()
        bootstrap_session.project_id = PROJECT_ID
        server.session_manager.get.return_value = bootstrap_session

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.execution.resolve_session_reference",
                return_value="resolved-header-uuid",
            ) as mock_resolve,
            patch(
                "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
                return_value=SeededContextTokens(),
            ) as mock_helper,
        ):
            _set_context_for_request(server, {"session_id": "#5"}, request)

        mock_resolve.assert_called_once_with(server.session_manager.db, header_session_uuid)
        # The derived project_id is fed back into the helper
        assert mock_helper.call_args.kwargs["project_ref"] == PROJECT_ID

    def test_hash_n_no_project_header_bootstraps_from_unique_header_hash_ref(self) -> None:
        """A stdio wrapper #N can provide scope when it is unique across projects."""
        db = MagicMock()
        db.fetchall.return_value = [{"project_id": PROJECT_ID}]
        server = _make_server(db)
        request = _make_request(session_id="#7")  # no project_id header

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            _set_context_for_request(server, {"session_id": "#5"}, request)

        db.fetchall.assert_called_once()
        assert db.fetchall.call_args.args[1] == (7,)
        assert mock_helper.call_args.kwargs["project_ref"] == PROJECT_ID

    def test_hash_n_header_ref_without_unique_project_stays_unscoped(self) -> None:
        """Ambiguous #N refs still require an explicit project header."""
        db = MagicMock()
        db.fetchall.return_value = [
            {"project_id": PROJECT_ID},
            {"project_id": str(uuid.uuid4())},
        ]
        server = _make_server(db)
        request = _make_request(session_id="#7")  # no project_id header

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            _set_context_for_request(server, {"session_id": "#5"}, request)

        assert mock_helper.call_args.kwargs["project_ref"] is None

    def test_no_session_id_forwards_header_project_ref(self) -> None:
        """No session ref → helper receives only the x-gobby-project-id header."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            _set_context_for_request(server, {}, request)

        kwargs = mock_helper.call_args.kwargs
        assert kwargs["session_ref"] is None
        assert kwargs["project_ref"] == PROJECT_ID
        assert kwargs["project_ref_is_fallback"] is True

    def test_header_session_id_also_forwarded(self) -> None:
        """#N from X-Gobby-Session-Id header is forwarded when no arg session_id."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID, session_id="#7")

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            _set_context_for_request(server, {}, request)

        assert mock_helper.call_args.kwargs["session_ref"] == "#7"

    def test_set_contexts_unresolvable_uuid_does_not_plant_session_context(self) -> None:
        """Helper returns empty tokens → no session ContextVar planted."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),  # resolved_session_id is None
        ) as mock_helper:
            tokens = _set_context_for_request(server, {"session_id": "bogus"}, request)

        assert mock_helper.called
        assert tokens.session_token is None
        assert tokens.resolved_session_id is None
