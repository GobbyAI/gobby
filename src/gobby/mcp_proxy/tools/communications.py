import asyncio
import logging
import mimetypes
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from gobby.communications.manager import CommunicationsManager
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory_scope import get_current_project_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_resolution import resolve_session_reference
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from gobby.utils.datetime import utc_now
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)


def _is_project_seq_ref(ref: str) -> bool:
    """True for #N, a bare sequence number, and <project>#N."""
    if ref.startswith("#"):
        return ref[1:].isdigit()
    project, separator, seq = ref.partition("#")
    if separator:
        return bool(project) and "#" not in project and seq.isdigit()
    return ref.isdigit()


def _is_within(path: Path, root: Path) -> bool:
    """True when resolved ``path`` lies under ``root`` once ``root`` resolves strictly."""
    try:
        return path.is_relative_to(root.expanduser().resolve(strict=True))
    except OSError:
        return False


def create_communications_registry(
    communications_manager: CommunicationsManager,
    db: HubDatabase | None = None,
    workspace_root: Path | None = None,
) -> InternalToolRegistry:
    """Create a registry with communication tools."""
    registry = InternalToolRegistry(
        name="gobby-communications",
        description=(
            "Tools for interacting with external communication channels "
            "(e.g., Slack, Discord, Email) - send_message, send_attachment, "
            "list_channels, get_messages, and channel management"
        ),
    )

    @registry.tool(
        description=(
            "Send a message to a communication channel. session_id defaults to the calling "
            "session when available. For Telegram clarification or approval "
            "prompts, pass inline_keyboard as rows of {text, value} buttons with a session_id; "
            "the selected value returns to that session. callback_ttl_seconds sets how long "
            "those buttons stay answerable and must be 1-3600. For Telegram text messages, "
            "link_preview_options overrides the channel's preview defaults."
        )
    )
    async def send_message(
        channel: str,
        content: str,
        session_id: str | None = None,
        thread_id: str | None = None,
        content_type: str = "text",
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        callback_ttl_seconds: int = 300,
        link_preview_options: dict[str, bool | str] | None = None,
    ) -> dict[str, Any]:
        """Send a message via the CommunicationsManager."""
        try:
            metadata: dict[str, Any] | None = None
            if (
                thread_id
                or content_type != "text"
                or inline_keyboard is not None
                or link_preview_options is not None
            ):
                metadata = {}
                if thread_id:
                    metadata["thread_id"] = thread_id
                if content_type != "text":
                    metadata["content_type"] = content_type
                if inline_keyboard is not None:
                    metadata["inline_keyboard"] = inline_keyboard
                    metadata["callback_ttl_seconds"] = callback_ttl_seconds
                    project_id = get_current_project_id()
                    if project_id is not None:
                        metadata["callback_project_id"] = project_id
                if link_preview_options is not None:
                    metadata["link_preview_options"] = link_preview_options

            target_session = session_id if session_id is not None else get_current_session_id()
            if target_session is not None and _is_project_seq_ref(target_session):
                if db is None:
                    raise ValueError(
                        f"Cannot resolve session '{target_session}': project storage is unavailable"
                    )
                target_session = resolve_session_reference(
                    db,
                    target_session,
                    get_current_project_id(),
                )

            msg = await communications_manager.send_message(
                channel_name=channel,
                content=content,
                session_id=target_session,
                metadata=metadata,
            )
            return {"success": msg.status == "sent", "message_id": msg.id, "error": msg.error}
        except ValueError as e:
            # Invalid caller input goes back to the caller; it is not a daemon fault.
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(
        description="Attach the calling live session to one Telegram conversation. Use dm:<chat_id>, group:<chat_id>, or topic:<chat_id>:<thread_id>."
    )
    def attach_conversation(channel: str, conversation_id: str) -> dict[str, Any]:
        session_id = get_current_session_id()
        if session_id is None:
            return {"success": False, "error": "Calling session context is required"}
        try:
            communications_manager.attach_conversation(channel, conversation_id, session_id)
            return {"success": True, "session_id": session_id, "conversation_id": conversation_id}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    @registry.tool(description="Detach the calling session from a Telegram conversation.")
    def detach_conversation(channel: str, conversation_id: str) -> dict[str, Any]:
        session_id = get_current_session_id()
        if session_id is None:
            return {"success": False, "error": "Calling session context is required"}
        try:
            communications_manager.detach_conversation(channel, conversation_id, session_id)
            return {"success": True}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    async def _registered_worktree_roots(
        project_context: dict[str, Any] | None, requested: Path, resolved: Path
    ) -> list[Path]:
        """Active or stale worktrees of the caller's project registered at an ancestor path.

        One exact-path lookup per ancestor of the requested and resolved file paths, so
        the lookup is bounded by path depth rather than the project's worktree count.
        """
        from gobby.app_context import get_app_context

        project_id = project_context.get("id") if project_context else None
        if db is None or not project_id:
            return []
        live = {WorktreeStatus.ACTIVE.value, WorktreeStatus.STALE.value}

        def lookup() -> list[Path]:
            manager = LocalWorktreeManager(db)
            roots: list[Path] = []
            for ancestor in dict.fromkeys([*requested.parents, *resolved.parents]):
                worktree = manager.get_by_path(str(ancestor))
                if worktree and worktree.project_id == project_id and worktree.status in live:
                    roots.append(Path(worktree.worktree_path))
            return roots

        app_context = get_app_context()
        if app_context is not None and app_context.db_executor is not None:
            roots: list[Path] = await app_context.run_db(lookup)
            return roots
        return await asyncio.to_thread(lookup)

    @registry.tool(
        description=(
            "Send an existing local file to a communication channel. The file must be "
            "inside the project checkout or one of its active or stale registered worktrees."
        )
    )
    async def send_attachment(
        channel: str,
        file_path: str,
        caption: str = "",
        session_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and send a local image or document."""
        try:
            requested_path = Path(file_path).expanduser().absolute()
            resolved_path = requested_path.resolve(strict=True)
            if not resolved_path.is_file():
                return {"success": False, "error": f"Attachment path is not a file: {file_path}"}
            project_context = get_project_context()
            context_root = project_context.get("project_path") if project_context else None
            configured_root = workspace_root or (
                Path(context_root) if isinstance(context_root, str) and context_root else None
            )
            if configured_root is None:
                return {"success": False, "error": "Attachment workspace is unavailable"}
            if not _is_within(resolved_path, configured_root) and not any(
                _is_within(resolved_path, root)
                for root in await _registered_worktree_roots(
                    project_context, requested_path, resolved_path
                )
            ):
                return {
                    "success": False,
                    "error": f"Attachment path is outside the workspace: {file_path}",
                }

            resolved_content_type = content_type
            if not resolved_content_type:
                resolved_content_type = (
                    mimetypes.guess_type(filename or resolved_path.name)[0]
                    or "application/octet-stream"
                )

            message, attachment = await communications_manager.send_attachment(
                channel_name=channel,
                file_path=resolved_path,
                filename=filename,
                content_type=resolved_content_type,
                content=caption,
                session_id=session_id,
                metadata=metadata,
            )
            return {
                "success": message.status == "sent",
                "message": {
                    "id": message.id,
                    "status": message.status,
                    "platform_message_id": message.platform_message_id,
                    "content": message.content,
                    "error": message.error,
                },
                "attachment": {
                    "id": attachment.id,
                    "message_id": attachment.message_id,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size_bytes": attachment.size_bytes,
                    "platform_url": attachment.platform_url,
                },
            }
        except (FileNotFoundError, OSError) as e:
            return {"success": False, "error": f"Invalid attachment path: {e}"}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(
        description="List configured communication channels and their status.", read_only=True
    )
    def list_channels() -> dict[str, Any]:
        """List all configured communication channels."""
        try:
            channels = communications_manager.list_channels()
            result = []
            for ch in channels:
                status = communications_manager.get_channel_status(ch.name)
                result.append(
                    {
                        "id": ch.id,
                        "name": ch.name,
                        "type": ch.channel_type,
                        "enabled": ch.enabled,
                        "status": status,
                        "project_id": (
                            ch.config_json.get("responder", {}).get("project_id")
                            if isinstance(ch.config_json.get("responder"), dict)
                            else None
                        ),
                    }
                )
            return {"success": True, "channels": result}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="Get message history for a channel.", read_only=True)
    def get_messages(
        channel: str | None = None,
        session_id: str | None = None,
        direction: Literal["inbound", "outbound"] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query message history."""
        try:
            channel_id = None
            if channel:
                ch = communications_manager.get_channel_by_name(channel)
                if ch:
                    channel_id = ch.id
                else:
                    return {"success": False, "error": f"Channel '{channel}' not found"}

            messages = communications_manager.list_messages(
                channel_id=channel_id,
                session_id=session_id,
                direction=direction,
                limit=limit,
            )
            return {
                "success": True,
                "messages": [
                    {
                        "id": m.id,
                        "channel_id": m.channel_id,
                        "direction": m.direction,
                        "content": m.content,
                        "created_at": m.created_at,
                        "session_id": m.session_id,
                    }
                    for m in messages
                ],
            }
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="Add a new communication channel.")
    async def add_channel(
        channel_type: str,
        name: str,
        config: dict[str, Any],
        secrets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a new communication channel."""
        try:
            ch = await communications_manager.add_channel(
                channel_type=channel_type,
                name=name,
                config=config,
                secrets=secrets,
            )
            channel = communications_manager.channel_to_dict(ch)
            return {
                "success": channel["active"],
                "channel_id": ch.id,
                "active": channel["active"],
                "init_error": channel["init_error"],
                "channel": channel,
            }
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="Remove a communication channel.")
    async def remove_channel(
        name: str,
    ) -> dict[str, Any]:
        """Remove a communication channel."""
        try:
            await communications_manager.remove_channel(name=name)
            return {"success": True}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(
        description=(
            "Set a channel's default Gobby project by UUID or exact project name. "
            "The next responder turn switches to that project."
        )
    )
    async def set_channel_project(channel: str, project: str) -> dict[str, Any]:
        """Persist the project used by future responder turns on one channel."""
        try:
            if db is None:
                return {"success": False, "error": "Project storage is unavailable"}
            configured_channel = communications_manager.get_channel_by_name(channel)
            if configured_channel is None:
                return {"success": False, "error": f"Channel '{channel}' not found"}

            project_manager = LocalProjectManager(db)
            resolved = await asyncio.to_thread(project_manager.resolve_ref, project)
            if resolved is None:
                return {"success": False, "error": f"Project '{project}' not found"}

            config = dict(configured_channel.config_json)
            raw_responder = config.get("responder")
            responder = dict(raw_responder) if isinstance(raw_responder, dict) else {}
            responder["project_id"] = resolved.id
            config["responder"] = responder
            updated = replace(
                configured_channel,
                config_json=config,
                updated_at=utc_now(),
            )
            await communications_manager.update_channel(updated)
            project_path = None
            try:
                from gobby.storage.project_checkouts import require_root
                from gobby.storage.workspace_machine_scope import require_local_machine_id

                machine_id = require_local_machine_id(
                    None, resource_kind="project_checkout", resource_id=resolved.id
                )
                project_path = require_root(db, resolved.id, machine_id)
            except (ValueError, RuntimeError):
                project_path = None
            return {
                "success": True,
                "channel": channel,
                "project_id": resolved.id,
                "project_name": resolved.name,
                "project_path": project_path,
            }
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(
        description="Send a proactive message to a Teams conversation (requires prior inbound message)."
    )
    async def send_proactive_message(
        channel: str,
        conversation_id: str,
        content: str,
        content_type: str = "text",
    ) -> dict[str, Any]:
        """Send a proactive message using a stored ConversationReference."""
        try:
            message = await communications_manager.send_proactive(
                channel_name=channel,
                conversation_id=conversation_id,
                content=content,
                content_type=content_type,
            )
            return {
                "success": message.status == "sent",
                "message_id": message.id,
                "platform_message_id": message.platform_message_id,
                "status": message.status,
                "error": message.error,
            }
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="Manually link an external user to a Gobby session.")
    def link_identity(channel: str, external_user_id: str, session_id: str) -> dict[str, Any]:
        """Link an external user to a Gobby session."""
        try:
            ch = communications_manager.get_channel_by_name(channel)
            if not ch:
                return {"success": False, "error": f"Channel '{channel}' not found"}

            identity = communications_manager.get_identity_by_external(ch.id, external_user_id)
            if not identity:
                return {"success": False, "error": f"Identity for '{external_user_id}' not found"}

            communications_manager.update_identity_session(identity.id, session_id)
            return {"success": True, "identity_id": identity.id}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="List identity mappings with optional filters.", read_only=True)
    def list_identities(
        session_id: str | None = None, channel: str | None = None
    ) -> dict[str, Any]:
        """List identity mappings with optional filters."""
        try:
            channel_id = None
            if channel:
                ch = communications_manager.get_channel_by_name(channel)
                if ch:
                    channel_id = ch.id
                else:
                    return {"success": False, "error": f"Channel '{channel}' not found"}

            identities = communications_manager.list_identities(channel_id=channel_id)
            if session_id:
                identities = [i for i in identities if i.session_id == session_id]

            return {
                "success": True,
                "identities": [
                    {
                        "id": i.id,
                        "channel_id": i.channel_id,
                        "external_user_id": i.external_user_id,
                        "external_username": i.external_username,
                        "session_id": i.session_id,
                    }
                    for i in identities
                ],
            }
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="Remove session link from an identity.")
    def unlink_identity(identity_id: str) -> dict[str, Any]:
        """Remove session link from an identity."""
        try:
            communications_manager.update_identity_session(identity_id, None)
            return {"success": True}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    return registry
