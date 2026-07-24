"""Identity manager for communication channels."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.communications.models import CommsIdentity
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.config.communications import CommunicationsConfig
    from gobby.storage.communications import LocalCommunicationsStore
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """Sender identity plus the session selected for one inbound message."""

    identity: CommsIdentity
    session_id: str | None


class IdentityManager:
    """Manages mapping between external platform IDs and Gobby identities/sessions."""

    def __init__(
        self,
        store: LocalCommunicationsStore,
        session_store: SessionManager,
        config: CommunicationsConfig,
    ) -> None:
        """Initialize the identity manager.

        Args:
            store: Local communications storage.
            session_store: Session manager for auto-creating sessions.
            config: Communications configuration.
        """
        self._store = store
        self._session_store = session_store
        self._config = config

    def bridge_identity(self, identity_id: str, session_id: str) -> None:
        """Link existing identity to a session."""
        identity = self._store.get_identity(identity_id)
        if identity:
            identity.session_id = session_id
            self._store.update_identity(identity)

    def resolve_identity(
        self,
        channel_id: str,
        external_user_id: str,
        external_username: str | None = None,
        metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> CommsIdentity:
        """Resolve identity and auto-create/link session if needed.

        Args:
            channel_id: Internal channel UUID.
            external_user_id: Platform-specific user ID.
            external_username: Optional platform-specific username.
            metadata: Optional metadata to merge into identity (e.g. conversation_reference).
            project_id: Optional project ID for auto-created sessions.

        Returns:
            The resolved CommsIdentity.
        """
        return self._resolve(
            channel_id=channel_id,
            external_user_id=external_user_id,
            external_username=external_username,
            metadata=metadata,
            project_id=project_id,
            group_chat_id=None,
        ).identity

    def resolve_inbound_identity(
        self,
        channel_id: str,
        external_user_id: str,
        external_username: str | None = None,
        metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
        group_chat_id: str | None = None,
    ) -> IdentityResolution:
        """Resolve sender attribution and the effective inbound conversation session."""
        return self._resolve(
            channel_id=channel_id,
            external_user_id=external_user_id,
            external_username=external_username,
            metadata=metadata,
            project_id=project_id,
            group_chat_id=group_chat_id,
        )

    def _resolve(
        self,
        *,
        channel_id: str,
        external_user_id: str,
        external_username: str | None,
        metadata: dict[str, Any] | None,
        project_id: str | None,
        group_chat_id: str | None,
    ) -> IdentityResolution:
        identity = self._store.get_identity_by_external(channel_id, external_user_id)
        link_session_to_identity = group_chat_id is None
        if link_session_to_identity:
            session_external_id = f"comms:{channel_id}:{external_user_id}"
            session_title = f"Comms: {external_username or external_user_id}"
        else:
            session_external_id = f"comms:{channel_id}:group:{group_chat_id}"
            session_title = f"Comms group: {group_chat_id}"

        session_id = None
        if link_session_to_identity and identity and identity.session_id:
            session_id = identity.session_id

        if not session_id and self._config.auto_create_sessions:
            session = self._session_store.register(
                external_id=session_external_id,
                machine_id="comms",
                source="comms",
                project_id=project_id,
                title=session_title,
            )
            session_id = session.id

        if identity:
            needs_update = False
            if link_session_to_identity and session_id and identity.session_id != session_id:
                identity.session_id = session_id
                needs_update = True
            if external_username and identity.external_username != external_username:
                identity.external_username = external_username
                needs_update = True

            # Merge metadata if provided
            if metadata:
                for k, v in metadata.items():
                    if identity.metadata_json.get(k) != v:
                        identity.metadata_json[k] = v
                        needs_update = True

            if needs_update:
                self._store.update_identity(identity)
        else:
            # Store generates the id on insert.
            now = utc_now()
            identity = CommsIdentity(
                id="",
                channel_id=channel_id,
                external_user_id=external_user_id,
                external_username=external_username,
                session_id=session_id if link_session_to_identity else None,
                created_at=now,
                updated_at=now,
                metadata_json=metadata or {},
            )
            identity = self._store.create_identity(identity)

        return IdentityResolution(identity=identity, session_id=session_id)

    def get_identity_by_session(self, channel_id: str, session_id: str) -> CommsIdentity | None:
        """Find the identity associated with a session on a specific channel."""
        identities = self._store.list_identities(channel_id=channel_id)
        return next((i for i in identities if i.session_id == session_id), None)
