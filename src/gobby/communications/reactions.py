"""Reaction handling for communications."""

import asyncio
import logging
from typing import Any

# Import ServiceContainer if available, or just Any for typing
from gobby.app_context import ServiceContainer
from gobby.storage.communications import LocalCommunicationsStore
from gobby.workflows.pipeline.gatekeeper import ApprovalManager

logger = logging.getLogger(__name__)


class ReactionHandler:
    """Handles platform-native emoji reactions and maps them to actions."""

    def __init__(
        self,
        store: LocalCommunicationsStore,
        service_container: ServiceContainer,
    ) -> None:
        """Initialize the reaction handler.

        Args:
            store: Storage manager to look up messages and rules.
            service_container: Access to other services (pipelines, tasks, etc.).
        """
        self._store = store
        self._services = service_container

        # Default reaction mappings
        self._default_mappings = {
            "+1": "approve",
            "thumbsup": "approve",
            "white_check_mark": "approve",
            "-1": "reject",
            "thumbsdown": "reject",
            "x": "reject",
        }

    async def handle_reaction(
        self, channel_name: str, message_id: str, reaction: str, user_id: str
    ) -> None:
        """Process a reaction added to a message.

        Args:
            channel_name: Name of the channel the reaction occurred on.
            message_id: The platform message ID that was reacted to.
            reaction: The emoji or reaction string.
            user_id: The platform user ID who reacted.
        """
        # 1. Look up the original message by platform_message_id
        # We need a method to get message by platform ID
        message = await asyncio.to_thread(
            self._store.get_message_by_platform_id,
            channel_name,
            message_id,
        )
        if not message:
            logger.debug(f"Reaction {reaction} on unknown message {message_id} in {channel_name}")
            return

        # 2. Extract action mapping from message metadata or routing rules
        action = self._resolve_action(reaction, message.metadata_json)
        if not action:
            logger.debug(f"No action mapped for reaction {reaction} on message {message_id}")
            return

        # 3. Look up identity to map to Gobby user
        identity = await asyncio.to_thread(
            self._store.get_identity_by_external,
            message.channel_id,
            user_id,
        )
        if not identity:
            logger.warning(f"Unknown user {user_id} reacted to message {message_id}")
            return

        logger.info(f"Executing action {action} triggered by reaction {reaction} from {user_id}")

        # 4. Execute the action
        await self._execute_action(action, message, identity)

    def _resolve_action(self, reaction: str, message_metadata: dict[str, Any]) -> str | None:
        """Resolve a reaction to an action name."""
        # Normalize reaction
        normalized = reaction.strip(":").lower()

        # Check message metadata for specific overrides
        custom_mappings = message_metadata.get("reaction_mappings", {})
        if normalized in custom_mappings:
            return str(custom_mappings[normalized])

        # Fallback to defaults
        result: str | None = self._default_mappings.get(normalized)
        return result

    async def _execute_action(self, action: str, message: Any, identity: Any) -> None:
        """Execute the mapped action.

        Args:
            action: The action string (e.g., "approve", "reject").
            message: The CommsMessage that was reacted to.
            identity: The CommsIdentity of the user who reacted.
        """
        if action == "approve":
            await self._handle_approval(message, identity, approved=True)
        elif action == "reject":
            await self._handle_approval(message, identity, approved=False)
        else:
            logger.warning(f"Unknown action: {action}")

    async def _handle_approval(self, message: Any, identity: Any, approved: bool) -> None:
        """Handle pipeline approval actions."""
        # Check if message has approval context
        context = message.metadata_json.get("approval_context", {})
        pipeline_run_id = context.get("run_id")
        step_id = context.get("step_id")
        token = context.get("token")

        if not pipeline_run_id or not step_id or not token:
            logger.debug("Message does not contain pipeline approval context")
            return

        approval_manager = self._get_approval_manager()
        if not approval_manager:
            logger.error("Pipeline approval manager not available to process approval")
            return

        approver_id = str(identity.id)
        if approved:
            logger.info(f"Approving pipeline {pipeline_run_id} step {step_id}")
            await approval_manager.approve_step(token, approved_by=approver_id)
        else:
            logger.info(f"Rejecting pipeline {pipeline_run_id} step {step_id}")
            await approval_manager.reject_step(token, rejected_by=approver_id)

    def _get_approval_manager(self) -> Any | None:
        """Return the pipeline gatekeeper used for token-based approvals."""
        executor = self._services.pipeline_executor
        if executor and getattr(executor, "approval_manager", None):
            return executor.approval_manager

        execution_manager = self._services.pipeline_execution_manager
        if not execution_manager:
            return None

        run_db = getattr(self._services, "run_db", None)
        return ApprovalManager(execution_manager=execution_manager, run_db=run_db)
