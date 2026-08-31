"""Tests for _set_context_for_request in request_context.py.

After Change 2b, _set_context_for_request delegates to the shared
resolve_and_seed_contexts helper. These tests verify the HTTP-specific
bootstrap (deriving project_id from the X-Gobby-Session-Id header when
the incoming ref is #N/numeric) and that wrapper session headers own
workflow/session context when body arguments target another session.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.routes.mcp.endpoints.request_context import _set_context_for_request
from gobby.utils.session_context import TERMINAL_CONTEXT_HEADER, SeededContextTokens

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SESSION_UUID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())


def _exception_detail(exc: HTTPException) -> dict[str, Any]:
    return cast(dict[str, Any], exc.detail)


def _make_server(db: MagicMock | None = None) -> MagicMock:
    server = MagicMock()
    server.session_manager = MagicMock()
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    if db is not None:
        server.session_manager.db = db
    return server


def _make_request(
    project_id: str | None = None,
    session_id: str | None = None,
    caller_project_id: str | None = None,
    *,
    wrapper: bool = False,
    terminal_context: str | None = None,
) -> MagicMock:
    request = MagicMock()
    headers: dict[str, str] = {}
    if project_id:
        headers["x-gobby-project-id"] = project_id
    if caller_project_id:
        headers["x-gobby-caller-project-id"] = caller_project_id
    if session_id:
        headers["x-gobby-session-id"] = session_id
    if wrapper:
        headers[MCP_WRAPPER_PROTOCOL_VERSION_HEADER] = MCP_WRAPPER_PROTOCOL_VERSION
    if terminal_context is not None:
        headers[TERMINAL_CONTEXT_HEADER] = terminal_context
    request.headers = headers
    return request


class TestSetContextForRequest:
    """Tests for _set_context_for_request after helper extraction."""

    async def test_hash_n_ref_forwarded_to_helper_with_header_project_scope(self) -> None:
        """#N reference is forwarded to resolve_and_seed_contexts with the header project scope."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_session_id=SESSION_UUID),
        ) as mock_helper:
            await _set_context_for_request(server, {"session_id": "#5"}, request)

        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        assert kwargs["session_ref"] == "#5"
        assert kwargs["project_ref"] == PROJECT_ID
        assert kwargs["session_ref_origin"] == "ambient"
        assert kwargs["project_ref_is_fallback"] is True

    async def test_uuid_session_id_forwarded_verbatim_to_helper(self) -> None:
        """UUID-shaped refs are handed to the helper; the resolver resolves external_id → id."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_session_id=SESSION_UUID),
        ) as mock_helper:
            external_uuid = str(uuid.uuid4())
            await _set_context_for_request(server, {"session_id": external_uuid}, request)

        # Flip of the old lock-in: UUID-shaped refs no longer bypass resolution.
        assert mock_helper.call_args.kwargs["session_ref"] == external_uuid
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "ambient"

    async def test_hash_n_no_project_header_bootstraps_from_header_session(self) -> None:
        """#N ref without x-gobby-project-id derives project from x-gobby-session-id."""
        server = _make_server()
        header_session_uuid = str(uuid.uuid4())
        request = _make_request(session_id=header_session_uuid)  # no project_id header

        bootstrap_session = MagicMock()
        bootstrap_session.project_id = PROJECT_ID
        server.session_manager.get.return_value = bootstrap_session

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_session_reference",
                return_value="resolved-header-uuid",
            ) as mock_resolve,
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                return_value=SeededContextTokens(),
            ) as mock_helper,
        ):
            await _set_context_for_request(server, {"session_id": "#5"}, request)

        mock_resolve.assert_called_once_with(server.session_manager.db, header_session_uuid)
        # The derived project_id is fed back into the helper
        assert mock_helper.call_args.kwargs["session_ref"] == header_session_uuid
        assert mock_helper.call_args.kwargs["project_ref"] == PROJECT_ID
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "explicit"

    async def test_hash_n_no_project_header_bootstraps_from_unique_header_hash_ref(self) -> None:
        """A stdio wrapper #N can provide scope when it is unique across projects."""
        db = MagicMock()
        db.fetchall.return_value = [{"project_id": PROJECT_ID}]
        server = _make_server(db)
        request = _make_request(session_id="#7")  # no project_id header

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            await _set_context_for_request(server, {"session_id": "#5"}, request)

        db.fetchall.assert_called_once()
        assert db.fetchall.call_args.args[1] == (7,)
        assert mock_helper.call_args.kwargs["session_ref"] == "#7"
        assert mock_helper.call_args.kwargs["project_ref"] == PROJECT_ID
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "explicit"

    async def test_hash_n_header_ref_without_unique_project_stays_unscoped(self) -> None:
        """Ambiguous #N refs still require an explicit project header."""
        db = MagicMock()
        db.fetchall.return_value = [
            {"project_id": PROJECT_ID},
            {"project_id": str(uuid.uuid4())},
        ]
        server = _make_server(db)
        request = _make_request(session_id="#7")  # no project_id header

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            await _set_context_for_request(server, {"session_id": "#5"}, request)

        assert mock_helper.call_args.kwargs["project_ref"] is None
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "explicit"

    async def test_no_session_id_forwards_header_project_ref(self) -> None:
        """No session ref → helper receives only the x-gobby-project-id header."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            await _set_context_for_request(server, {}, request)

        kwargs = mock_helper.call_args.kwargs
        assert kwargs["session_ref"] is None
        assert kwargs["project_ref"] == PROJECT_ID
        assert kwargs["project_ref_is_fallback"] is True

    async def test_header_session_id_also_forwarded(self) -> None:
        """#N from X-Gobby-Session-Id header is forwarded when no arg session_id."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID, session_id="#7")

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            await _set_context_for_request(server, {}, request)

        assert mock_helper.call_args.kwargs["session_ref"] == "#7"
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "explicit"

    async def test_caller_project_header_scopes_wrapper_session_separately_from_target(
        self,
    ) -> None:
        """Cross-project calls keep caller session scope distinct from target project context."""
        server = _make_server()
        caller_project_id = str(uuid.uuid4())
        request = _make_request(
            project_id=PROJECT_ID,
            caller_project_id=caller_project_id,
            session_id="#7",
        )

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            await _set_context_for_request(server, {}, request)

        kwargs = mock_helper.call_args.kwargs
        assert kwargs["session_ref"] == "#7"
        assert kwargs["project_ref"] == PROJECT_ID
        assert kwargs["session_scope_ref"] == caller_project_id
        assert kwargs["session_ref_origin"] == "explicit"
        assert kwargs["project_ref_is_fallback"] is False

    async def test_header_session_wins_over_target_argument_session(self) -> None:
        """Body session_id remains the tool target; header is workflow context."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID, session_id="caller-session")

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),
        ) as mock_helper:
            await _set_context_for_request(server, {"session_id": "target-session"}, request)

        assert mock_helper.call_args.kwargs["session_ref"] == "caller-session"
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "explicit"

    async def test_set_contexts_unresolvable_uuid_does_not_plant_session_context(self) -> None:
        """Helper returns empty tokens → no session ContextVar planted."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID)

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(),  # resolved_session_id is None
        ) as mock_helper:
            tokens = await _set_context_for_request(server, {"session_id": "bogus"}, request)

        assert mock_helper.called
        assert mock_helper.call_args.kwargs["session_ref_origin"] == "ambient"
        assert tokens.session_token is None
        assert tokens.resolved_session_id is None

    async def test_wrapper_session_header_backfills_terminal_context(self) -> None:
        """An explicit wrapper session receives the wrapper's current terminal identity."""
        server = _make_server()
        terminal_context = {
            "parent_pid": 4242,
            "tmux_pane": "%142",
            "tmux_socket_path": "/tmp/tmux/default",
        }
        request = _make_request(
            project_id=PROJECT_ID,
            session_id="#7",
            wrapper=True,
            terminal_context=json.dumps(terminal_context),
        )

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_session_id=SESSION_UUID),
        ) as mock_helper:
            tokens = await _set_context_for_request(
                server,
                {"session_id": "target-session"},
                request,
            )

        assert tokens.resolved_session_id == SESSION_UUID
        assert mock_helper.call_args.kwargs["session_ref"] == "#7"
        server.session_manager.backfill_terminal_context.assert_called_once_with(
            SESSION_UUID,
            terminal_context,
        )
        server.session_manager.resolve_current_terminal_session.assert_not_called()

    async def test_wrapper_session_header_rejects_malformed_terminal_context(self) -> None:
        """An explicit session header does not bypass wrapper identity validation."""
        server = _make_server()
        request = _make_request(
            project_id=PROJECT_ID,
            session_id="#7",
            wrapper=True,
            terminal_context="{",
        )

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                new_callable=AsyncMock,
            ) as mock_helper,
            pytest.raises(HTTPException) as exc_info,
        ):
            await _set_context_for_request(server, {}, request)

        assert exc_info.value.status_code == 409
        assert _exception_detail(exc_info.value)["error_code"] == "SESSION_REQUIRED"
        mock_helper.assert_not_awaited()

    async def test_wrapper_target_session_does_not_bootstrap_caller_project(self) -> None:
        """A body session_id cannot seed wrapper caller enforcement context."""
        server = _make_server()
        request = _make_request(session_id=str(uuid.uuid4()), wrapper=True)

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_session_reference"
            ) as mock_resolve,
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                return_value=SeededContextTokens(resolved_session_id=SESSION_UUID),
            ) as mock_helper,
        ):
            await _set_context_for_request(server, {"session_id": "#5"}, request)

        assert mock_helper.call_args.kwargs["project_ref"] is None
        mock_resolve.assert_not_called()

    async def test_wrapper_terminal_context_uses_caller_project_scope(self) -> None:
        """Ambient resolution uses caller scope while target project remains independent."""
        server = _make_server()
        caller_project_id = str(uuid.uuid4())
        terminal_context = {
            "parent_pid": 4242,
            "tmux_pane": "%1",
            "tmux_socket_path": "/tmp/tmux/default",
            "tmux_window_id": "@7",
        }
        server.session_manager.resolve_current_terminal_session.return_value = SimpleNamespace(
            id=SESSION_UUID,
            external_id="external-session",
            project_id=caller_project_id,
        )
        request = _make_request(
            project_id=PROJECT_ID,
            caller_project_id=caller_project_id,
            wrapper=True,
            terminal_context=json.dumps(terminal_context),
        )

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_project_id=PROJECT_ID),
        ) as mock_helper:
            tokens = await _set_context_for_request(server, {}, request)

        server.run_db.assert_any_await(
            server.session_manager.resolve_current_terminal_session,
            caller_project_id,
            4242,
            terminal_context,
        )
        assert mock_helper.call_args.kwargs["session_ref"] is None
        assert mock_helper.call_args.kwargs["project_ref"] == PROJECT_ID
        assert tokens.resolved_session_id == SESSION_UUID
        assert tokens.resolved_project_id == PROJECT_ID

    async def test_wrapper_accepts_serialized_tmux_generation_fields(self) -> None:
        """Stdio capture includes tmux server generation; the daemon must not 409 it."""
        server = _make_server()
        terminal_context = {
            "parent_pid": 21035,
            "tmux_pane": "%123",
            "tmux_socket_path": "/private/tmp/tmux-501/default",
            "tmux_window_id": "@123",
            "tmux_session": "121",
            "tmux_server_pid": 6051,
            "tmux_server_start_time": 1787385464,
            "term_program": "tmux",
        }
        server.session_manager.resolve_current_terminal_session.return_value = SimpleNamespace(
            id=SESSION_UUID,
            external_id="external-session",
            project_id=PROJECT_ID,
        )
        request = _make_request(
            project_id=PROJECT_ID,
            wrapper=True,
            terminal_context=json.dumps(terminal_context),
        )

        with patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            return_value=SeededContextTokens(resolved_project_id=PROJECT_ID),
        ):
            tokens = await _set_context_for_request(server, {}, request)

        server.session_manager.resolve_current_terminal_session.assert_called_once()
        assert tokens.resolved_session_id == SESSION_UUID

    @pytest.mark.parametrize(
        ("terminal_context", "terminal_context_seen"),
        [
            (None, False),
            ("", True),
            ("{}", True),
            ("[]", True),
            ("null", True),
            ("{", True),
            ('{"unknown":"value"}', True),
        ],
    )
    async def test_wrapper_invalid_terminal_context_fails_closed(
        self,
        terminal_context: str | None,
        terminal_context_seen: bool,
    ) -> None:
        """Invalid or empty allowlisted context never reaches resolution."""
        server = _make_server()
        request = _make_request(
            project_id=PROJECT_ID,
            wrapper=True,
            terminal_context=terminal_context,
        )

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                new_callable=AsyncMock,
            ) as mock_helper,
            pytest.raises(HTTPException) as exc_info,
        ):
            await _set_context_for_request(server, {}, request)

        assert exc_info.value.status_code == 409
        assert _exception_detail(exc_info.value) == {
            "success": False,
            "error_code": "SESSION_REQUIRED",
            "error": "Wrapper caller session could not be resolved",
            "terminal_context_seen": terminal_context_seen,
        }
        mock_helper.assert_not_awaited()
        server.session_manager.resolve_current_terminal_session.assert_not_called()

    async def test_wrapper_unmatched_terminal_context_fails_closed(self) -> None:
        """A valid but unmatched terminal fingerprint is rejected before seeding."""
        server = _make_server()
        terminal_context = {"parent_pid": 4242, "tty": "/dev/ttys001"}
        server.session_manager.resolve_current_terminal_session.return_value = None
        request = _make_request(
            project_id=PROJECT_ID,
            wrapper=True,
            terminal_context=json.dumps(terminal_context),
        )

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                new_callable=AsyncMock,
            ) as mock_helper,
            pytest.raises(HTTPException) as exc_info,
        ):
            await _set_context_for_request(server, {}, request)

        assert exc_info.value.status_code == 409
        detail = _exception_detail(exc_info.value)
        assert detail["error_code"] == "SESSION_REQUIRED"
        assert detail["terminal_context_seen"] is True
        mock_helper.assert_not_awaited()

    async def test_wrapper_body_session_id_does_not_supply_caller_identity(self) -> None:
        """Wrapper tool arguments retain session_id without using it as caller context."""
        server = _make_server()
        request = _make_request(project_id=PROJECT_ID, wrapper=True)

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                new_callable=AsyncMock,
            ) as mock_helper,
            pytest.raises(HTTPException) as exc_info,
        ):
            await _set_context_for_request(server, {"session_id": "target-session"}, request)

        assert exc_info.value.status_code == 409
        assert _exception_detail(exc_info.value)["error_code"] == "SESSION_REQUIRED"
        mock_helper.assert_not_awaited()

    async def test_wrapper_unresolved_explicit_session_header_fails_closed(self) -> None:
        """An explicit wrapper header does not fall through to ambient resolution."""
        server = _make_server()
        request = _make_request(
            project_id=PROJECT_ID,
            session_id="missing-session",
            wrapper=True,
            terminal_context=json.dumps({"parent_pid": 4242}),
        )

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
                return_value=SeededContextTokens(),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _set_context_for_request(server, {}, request)

        assert exc_info.value.status_code == 409
        detail = _exception_detail(exc_info.value)
        assert detail["error_code"] == "SESSION_REQUIRED"
        assert detail["terminal_context_seen"] is True
        server.session_manager.resolve_current_terminal_session.assert_not_called()
