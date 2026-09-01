import asyncio
import logging
import mimetypes
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from gobby.communications.manager import CommunicationsManager
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.utils.datetime import utc_now
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)


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
            "list_channels, get_messages, channel management, and event subscriptions"
        ),
    )

    def resolve_subscription_project(
        project: str | None,
        *,
        global_scope: bool,
    ) -> str | None:
        if global_scope:
            if project is not None:
                raise ValueError("Choose either project scope or global scope")
            return None
        if project is not None:
            if db is None:
                raise ValueError("Project storage is unavailable")
            resolved = LocalProjectManager(db).resolve_ref(project)
            if resolved is None:
                raise ValueError(f"Project '{project}' not found")
            return resolved.id

        caller_session_id = get_current_session_id()
        if caller_session_id is None:
            raise ValueError("Calling session context is required")
        project_id = communications_manager.get_session_project_id(caller_session_id)
        if project_id is None:
            raise ValueError("Calling session project context is required")
        return project_id

    @registry.tool(
        description=(
            "Send a message to a communication channel. For Telegram clarification or approval "
            "prompts, pass inline_keyboard as rows of {text, value} buttons with a session_id; "
            "the selected value returns to that session. For Telegram text messages, "
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
                if link_preview_options is not None:
                    metadata["link_preview_options"] = link_preview_options

            msg = await communications_manager.send_message(
                channel_name=channel,
                content=content,
                session_id=session_id,
                metadata=metadata,
            )
            return {"success": msg.status == "sent", "message_id": msg.id, "error": msg.error}
        except Exception as e:
            logger.exception("Communications tool error")
            return {"success": False, "error": str(e)}

    @registry.tool(description="Send an existing local file to a communication channel.")
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
            resolved_path = Path(file_path).expanduser().resolve(strict=True)
            if not resolved_path.is_file():
                return {"success": False, "error": f"Attachment path is not a file: {file_path}"}
            project_context = get_project_context()
            context_root = project_context.get("project_path") if project_context else None
            configured_root = workspace_root or (
                Path(context_root) if isinstance(context_root, str) and context_root else None
            )
            if configured_root is None:
                return {"success": False, "error": "Attachment workspace is unavailable"}
            resolved_root = configured_root.expanduser().resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root):
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

    @registry.tool(description="List configured communication channels and their status.")
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

    @registry.tool(description="Get message history for a channel.")
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
        description=(
            "Create an event subscription. Defaults to the calling session's project; "
            "set global_scope=true for explicit global routing."
        )
    )
    def create_event_subscription(
        name: str,
        channel: str,
        event_pattern: str,
        project: str | None = None,
        global_scope: bool = False,
        session_id: str | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create a validated event subscription."""
        try:
            project_id = resolve_subscription_project(project, global_scope=global_scope)
            rule = communications_manager.create_event_subscription(
                name=name,
                channel=channel,
                event_pattern=event_pattern,
                project_id=project_id,
                global_scope=global_scope,
                session_id=session_id,
                priority=priority,
                enabled=enabled,
            )
            return {
                "success": True,
                "subscription": communications_manager.event_subscription_to_dict(rule),
            }
        except (LookupError, ValueError) as e:
            return {"success": False, "error": str(e)}

    @registry.tool(description="List event subscriptions with exact administrative filters.")
    def list_event_subscriptions(
        channel: str | None = None,
        project: str | None = None,
        global_scope: bool | None = None,
        enabled: bool | None = None,
        event_pattern: str | None = None,
    ) -> dict[str, Any]:
        """List event subscriptions, including disabled entries by default."""
        try:
            project_id = None
            if project is not None:
                project_id = resolve_subscription_project(project, global_scope=False)
            rules = communications_manager.list_event_subscriptions(
                channel=channel,
                project_id=project_id,
                global_scope=global_scope,
                enabled=enabled,
                event_pattern=event_pattern,
            )
            return {
                "success": True,
                "subscriptions": [
                    communications_manager.event_subscription_to_dict(rule) for rule in rules
                ],
            }
        except (LookupError, ValueError) as e:
            return {"success": False, "error": str(e)}

    @registry.tool(description="Get one event subscription by ID.")
    def get_event_subscription(subscription_id: str) -> dict[str, Any]:
        """Get an event subscription."""
        try:
            rule = communications_manager.get_event_subscription(subscription_id)
            return {
                "success": True,
                "subscription": communications_manager.event_subscription_to_dict(rule),
            }
        except (LookupError, ValueError) as e:
            return {"success": False, "error": str(e)}

    @registry.tool(description="Partially update an event subscription by ID.")
    def update_event_subscription(
        subscription_id: str,
        name: str | None = None,
        channel: str | None = None,
        event_pattern: str | None = None,
        project: str | None = None,
        global_scope: bool | None = None,
        session_id: str | None = None,
        clear_session: bool = False,
        priority: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Partially update an event subscription."""
        try:
            changes: dict[str, Any] = {}
            for key, value in (
                ("name", name),
                ("channel", channel),
                ("event_pattern", event_pattern),
                ("priority", priority),
                ("enabled", enabled),
            ):
                if value is not None:
                    changes[key] = value
            if project is not None:
                changes["project_id"] = resolve_subscription_project(
                    project,
                    global_scope=False,
                )
                changes["global_scope"] = False
            elif global_scope is True:
                changes["global_scope"] = True
            if session_id is not None or clear_session:
                changes["session_id"] = session_id
            if not changes:
                raise ValueError("No updates specified")
            rule = communications_manager.update_event_subscription(subscription_id, **changes)
            return {
                "success": True,
                "subscription": communications_manager.event_subscription_to_dict(rule),
            }
        except (LookupError, ValueError) as e:
            return {"success": False, "error": str(e)}

    @registry.tool(description="Delete an event subscription by ID.")
    def delete_event_subscription(subscription_id: str) -> dict[str, Any]:
        """Delete an event subscription."""
        try:
            communications_manager.delete_event_subscription(subscription_id)
            return {"success": True, "deleted": subscription_id}
        except (LookupError, ValueError) as e:
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

    @registry.tool(description="List identity mappings with optional filters.")
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
