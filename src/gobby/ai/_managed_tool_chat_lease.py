"""ToolChat adapter for request-scoped managed credential leases."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from gobby.ai._tool_chat_contracts import ToolChatRequest
from gobby.ai._tool_chat_service import ToolChatLeaseFactory
from gobby.storage.managed_credentials import (
    CredentialAuthorizationError,
    ManagedCredentialManager,
)
from gobby.utils.local_token import read_local_api_token
from gobby.utils.machine_id import get_machine_id

_MAX_TOOL_ROLE_LIFETIME_SECONDS = 3540.0


async def _revoke_shielded(manager: ManagedCredentialManager, execution_id: UUID) -> None:
    revoke_task = asyncio.create_task(
        asyncio.to_thread(
            manager.revoke,
            execution_id,
            reason="tool-request-finally",
        )
    )
    try:
        await asyncio.shield(revoke_task)
    except asyncio.CancelledError:
        await revoke_task
        raise


def build_managed_tool_chat_lease_factory(
    manager: ManagedCredentialManager,
) -> ToolChatLeaseFactory:
    """Build an outer ToolChat lease backed by the daemon credential manager."""

    @asynccontextmanager
    async def lease(
        request: ToolChatRequest,
        timeout_seconds: float,
    ) -> AsyncIterator[ToolChatRequest]:
        if request.session_id is None:
            raise CredentialAuthorizationError("authenticated session is required")
        lifetime_seconds = min(timeout_seconds, _MAX_TOOL_ROLE_LIFETIME_SECONDS)
        issue_task = asyncio.create_task(
            asyncio.to_thread(
                manager.issue_tool_request,
                session_id=request.session_id,
                requested_project_path=request.project_path,
                expires_at=datetime.now(UTC) + timedelta(seconds=lifetime_seconds),
            )
        )
        try:
            tool_credential = await asyncio.shield(issue_task)
        except asyncio.CancelledError:
            tool_credential = await issue_task
            await _revoke_shielded(
                manager,
                tool_credential.credential.managed_execution_id,
            )
            raise
        execution_id = tool_credential.credential.managed_execution_id
        try:
            from gobby.agents.code_index import (
                IndexInventoryError,
                _active_deployment_grant_context,
                _signed_grant_from_credential,
            )
            from gobby.runtime_grants.launch import materialize_managed_launch

            operator_token = read_local_api_token()
            if operator_token is None:
                raise CredentialAuthorizationError("daemon operator capability is unavailable")
            try:
                context = _active_deployment_grant_context()
            except IndexInventoryError as exc:
                raise CredentialAuthorizationError(str(exc)) from exc
            grant = _signed_grant_from_credential(
                tool_credential.credential,
                machine_id=get_machine_id(),
                project_id=str(tool_credential.project_id),
                session_id=str(request.session_id),
                context=context,
                principal_kind="tool_chat",
            )
            remaining_seconds = (
                tool_credential.credential.expires_at - datetime.now(UTC)
            ).total_seconds()
            launch = materialize_managed_launch(
                grant,
                dest_dir=tool_credential.credential.bootstrap_path.parent,
                operator_token=operator_token,
                deadline_seconds=max(1.0, remaining_seconds),
            )
            scoped_request = replace(
                request,
                project_path=tool_credential.project_path,
                project_id=tool_credential.project_id,
                managed_execution_id=execution_id,
                credential_bootstrap_path=str(launch.grant_path),
                daemon_api_token=launch.env["GOBBY_AGENT_API_TOKEN"],
            )
            yield scoped_request
        finally:
            await _revoke_shielded(manager, execution_id)

    return lease
