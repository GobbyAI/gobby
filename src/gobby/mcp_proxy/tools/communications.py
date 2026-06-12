import logging
from typing import Any, Literal

from gobby.communications.manager import CommunicationsManager
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

logger = logging.getLogger(__name__)


def create_communications_registry(
    communications_manager: CommunicationsManager,
) -> InternalToolRegistry:
    """Create a registry with communication tools."""
    registry = InternalToolRegistry(
        name="gobby-communications",
        description="Tools for interacting with external communication channels (e.g., Slack, Discord, Email) - send_message, list_channels, get_messages, add_channel, remove_channel",
    )

    @registry.tool(description="Send a message to a communication channel.")
    async def send_message(
        channel: str,
        content: str,
        session_id: str | None = None,
        thread_id: str | None = None,
        content_type: str = "text",
    ) -> dict[str, Any]:
        """Send a message via the CommunicationsManager."""
        try:
            metadata = None
            if thread_id or content_type != "text":
                metadata = {}
                if thread_id:
                    metadata["thread_id"] = thread_id
                if content_type != "text":
                    metadata["content_type"] = content_type

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
            msg_id = await communications_manager.send_proactive(
                channel_name=channel,
                conversation_id=conversation_id,
                content=content,
                content_type=content_type,
            )
            return {"success": True, "message_id": msg_id}
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
