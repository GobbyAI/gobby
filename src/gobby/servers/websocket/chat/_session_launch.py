"""Post-hydration web-chat launch seam."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.adapters.plan_options import serialize_plan_accept_options
from gobby.agents.sandbox import SandboxConfig
from gobby.hooks.events import HookEventType
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat.runtime_manager import SandboxPolicySnapshot
from gobby.servers.websocket.db import run_db
from gobby.utils.json_helpers import json_dumps

from ._session_runtime import _get_runtime_external_id, _get_runtime_transcript_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionLaunchContext:
    """Immutable per-creation launch inputs assembled after path resolution."""

    sandbox: SandboxPolicySnapshot
    workspace_path: str


NATIVE_HOOK_AUTHORITY_PROVIDERS = frozenset({"agy"})
"""Providers whose native hook binary is the sole workflow-effect authority.

For these providers the managed session never routes ``BEFORE_AGENT``,
``BEFORE_TOOL``, ``AFTER_TOOL``, ``PRE_COMPACT``, or ``STOP`` through
``_fire_lifecycle`` (plan row 5.3.5); the callbacks stay unbound so the
invariant holds by construction.
"""


def uses_native_hook_authority(session: ChatSessionProtocol) -> bool:
    return getattr(session, "provider", None) in NATIVE_HOOK_AUTHORITY_PROVIDERS


def bind_session_lifecycle(owner: Any, session: ChatSessionProtocol, session_key: str) -> None:
    """Wire lifecycle and plan callbacks before backend start."""
    if not uses_native_hook_authority(session):
        _bind_managed_lifecycle(owner, session, session_key)

    async def _notify_mode_changed(mode: str, reason: str) -> None:
        msg = json_dumps(
            {
                "type": "mode_changed",
                "conversation_id": session_key,
                "mode": mode,
                "reason": reason,
            }
        )
        for ws, meta in list(owner.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid is not None and cid != session_key:
                continue
            try:
                await ws.send(msg)
            except (ConnectionClosed, ConnectionClosedError):
                pass

    session._on_mode_changed = _notify_mode_changed

    async def _notify_plan_ready(
        content: str | None, input_data: dict[str, Any], tool_use_id: str | None
    ) -> None:
        session._pending_plan_content = content
        allowed_prompts = input_data.get("allowedPrompts")
        session._pending_plan_allowed_prompts = (
            list(allowed_prompts)
            if isinstance(allowed_prompts, list)
            and all(isinstance(prompt, str) for prompt in allowed_prompts)
            else None
        )
        raw_source = getattr(session, "provider", None)
        plan_source = raw_source if isinstance(raw_source, str) else None
        msg = json_dumps(
            {
                "type": "plan_pending_approval",
                "conversation_id": session_key,
                "tool_call_id": tool_use_id,
                "plan_content": content,
                "allowed_prompts": session._pending_plan_allowed_prompts,
                "source": plan_source,
                "options": (serialize_plan_accept_options(plan_source) if plan_source else []),
            }
        )
        for ws, meta in list(owner.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid is not None and cid != session_key:
                continue
            try:
                await ws.send(msg)
            except (ConnectionClosed, ConnectionClosedError):
                pass

    session._on_plan_ready = _notify_plan_ready


def _bind_managed_lifecycle(owner: Any, session: ChatSessionProtocol, session_key: str) -> None:
    session._on_before_agent = lambda data: owner._fire_lifecycle(
        session_key, HookEventType.BEFORE_AGENT, data
    )
    session._on_pre_tool = lambda data: owner._fire_lifecycle(
        session_key, HookEventType.BEFORE_TOOL, data
    )
    session._on_post_tool = lambda data: owner._fire_lifecycle(
        session_key, HookEventType.AFTER_TOOL, data
    )
    session._on_pre_compact = lambda data: owner._fire_lifecycle(
        session_key, HookEventType.PRE_COMPACT, data
    )
    session._on_stop = lambda data: owner._fire_lifecycle(session_key, HookEventType.STOP, data)


def apply_launch_context(session: ChatSessionProtocol, context: SessionLaunchContext) -> None:
    """Thread the per-creation snapshot and resolved workspace into the session."""
    session.project_path = context.workspace_path
    session.sandbox_policy_hash = context.sandbox.policy_hash
    session.sandbox_config = context.sandbox.config.model_copy(deep=True)


async def start_hydrated_session(
    owner: Any,
    session: ChatSessionProtocol,
    context: SessionLaunchContext,
    *,
    session_key: str,
    effective_model: str | None,
    persona_selected: bool,
    pending_agent: str | None,
    pending_mode: str | None,
    agent_name: str,
    provider_name: str,
    session_manager: Any,
    existing_db_session: Any,
    project_context_changed: bool,
    effective_pid: str,
) -> ChatSessionProtocol:
    """Start the backend under the launch context and register the live session."""
    apply_launch_context(session, context)
    try:
        await session.start(model=effective_model)
    except Exception:
        if session.resume_session_id:
            logger.warning(
                "SDK resume failed for %s, starting fresh", session.resume_session_id[:8]
            )
            session.resume_session_id = None
            try:
                await session.start(model=effective_model)
            except Exception:
                await _cleanup_failed_start(session)
                raise
        else:
            await _cleanup_failed_start(session)
            raise

    existing_status = getattr(existing_db_session, "status", None)
    if (
        session_manager
        and session.db_session_id
        and existing_db_session
        and getattr(existing_db_session, "session_type", None) == "web_chat"
        and isinstance(existing_status, str)
        and existing_status != "active"
    ):
        try:
            activated = await run_db(
                owner,
                session_manager.activate_web_chat_session,
                session.db_session_id,
            )
            if activated is None or activated.status != "active":
                raise RuntimeError(
                    f"Web-chat session {session.db_session_id} is ineligible for activation"
                )
        except Exception:
            logger.exception(
                "Failed to activate hydrated web-chat session %s",
                session.db_session_id,
            )
            await _cleanup_failed_start(session)
            raise

    if session_manager and session.db_session_id:
        update_kwargs: dict[str, Any] = {}
        if project_context_changed:
            update_kwargs["project_id"] = effective_pid
        runtime_external_id = _get_runtime_external_id(session)
        if runtime_external_id and runtime_external_id != session_key:
            update_kwargs["external_id"] = runtime_external_id
        runtime_transcript_path = _get_runtime_transcript_path(session)
        if runtime_transcript_path:
            update_kwargs["transcript_path"] = runtime_transcript_path
        sandbox_metadata = session.sandbox_metadata
        if isinstance(sandbox_metadata, dict) and isinstance(
            sandbox_metadata.get("enforced"), bool
        ):
            update_kwargs["sandbox_enabled"] = sandbox_metadata["enforced"]
            update_kwargs["sandbox_policy_hash"] = sandbox_metadata.get("policy_hash")
            update_kwargs["terminal_context"] = {"sandbox": sandbox_metadata}
        workspace_path = context.workspace_path
        if workspace_path:
            update_kwargs["workspace_path"] = workspace_path
            existing_path = getattr(existing_db_session, "workspace_path", None)
            existing_gen = int(getattr(existing_db_session, "workspace_generation", 0) or 0)
            if existing_path != workspace_path:
                update_kwargs["workspace_generation"] = existing_gen + 1
        if update_kwargs:
            try:
                await run_db(
                    owner,
                    session_manager.update,
                    session.db_session_id,
                    **update_kwargs,
                )
            except Exception:
                logger.debug(
                    "Failed to persist runtime session metadata for web-chat session",
                    exc_info=True,
                )

    if session_manager and session.db_session_id and session.model:
        try:
            await run_db(
                owner,
                session_manager.update_model,
                session.db_session_id,
                session.model,
            )
        except Exception:
            logger.debug("Failed to persist selected model for web-chat session", exc_info=True)

    if persona_selected and session_manager and session.db_session_id:
        try:
            from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

            persona_result = await apply_persona_impl(
                agent=agent_name,
                db=session_manager.db,
                session_id=session.db_session_id,
                cli_source=provider_name,
            )
            if isinstance(persona_result, dict) and persona_result.get("success") is False:
                raise RuntimeError(
                    persona_result.get("error") or f"Failed to apply persona '{agent_name}'"
                )
        except Exception:
            logger.exception(
                "Failed to apply persona '%s' to session %s",
                agent_name,
                session_key,
            )
            await _cleanup_failed_start(session)
            raise

    registry = getattr(owner, "web_chat_session_registry", None)
    if registry is not None:
        registry.register(session_key, session)
    else:
        owner._chat_sessions[session_key] = session

    start_data: dict[str, Any] = {}
    if persona_selected:
        start_data["skip_default_agent_activation"] = True
    elif pending_agent:
        start_data["agent_name_override"] = pending_agent

    def _log_session_start_error(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("SESSION_START lifecycle hook failed: %s", exc)

    if provider_name not in NATIVE_HOOK_AUTHORITY_PROVIDERS:
        t = asyncio.create_task(
            owner._fire_lifecycle(session_key, HookEventType.SESSION_START, start_data)
        )
        t.add_done_callback(_log_session_start_error)

    if not pending_mode:
        mode_msg = json_dumps(
            {
                "type": "mode_changed",
                "conversation_id": session_key,
                "mode": session.chat_mode,
                "reason": "session_restored",
            }
        )
        for ws, meta in list(owner.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid is not None and cid != session_key:
                continue
            try:
                await ws.send(mode_msg)
            except (ConnectionClosed, ConnectionClosedError):
                pass

    return session


async def _cleanup_failed_start(session: ChatSessionProtocol) -> None:
    launch = getattr(session, "_sandbox_launch", None)
    cleanup = getattr(launch, "cleanup_cli_shim", None)
    if callable(cleanup):
        cleanup()
    try:
        await session.stop()
    except Exception:
        logger.exception(
            "Failed to stop web-chat runtime after failed start for session %s",
            getattr(session, "conversation_id", "?"),
        )


def snapshot_from_session(session: ChatSessionProtocol, policy_hash: str) -> SandboxPolicySnapshot:
    config = getattr(session, "sandbox_config", None)
    if not isinstance(config, SandboxConfig):
        config = SandboxConfig(enabled=False)
    return SandboxPolicySnapshot(
        config=config,
        policy_hash=getattr(session, "sandbox_policy_hash", None) or policy_hash,
    )
