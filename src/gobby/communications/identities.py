"""Identity manager for communication channels."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.communications.models import CommsIdentity

if TYPE_CHECKING:
    from gobby.config.communications import CommunicationsConfig
    from gobby.storage.communications import LocalCommunicationsStore
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


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

    def find_cross_channel_identity(self, external_username: str) -> str | None:
        """Search for matching identity on other channels by username pattern."""
        identities = self._store.find_identities_by_username(external_username)
        for identity in identities:
            if identity.session_id:
                return str(identity.session_id)
        return None

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
        identity = self._store.get_identity_by_external(channel_id, external_user_id)

        session_id = None
        if identity and identity.session_id:
            session_id = identity.session_id
        elif external_username:
            session_id = self.find_cross_channel_identity(external_username)

        if not session_id and self._config.auto_create_sessions:
            session = self._session_store.register(
                external_id=f"comms:{channel_id}:{external_user_id}",
                machine_id="comms",
                source="comms",
                project_id=project_id,
                title=f"Comms: {external_username or external_user_id}",
            )
            session_id = session.id

        if identity:
            needs_update = False
            if session_id and identity.session_id != session_id:
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
            # Store generates id/created_at/updated_at on insert
            identity = CommsIdentity(
                id="",
                channel_id=channel_id,
                external_user_id=external_user_id,
                external_username=external_username,
                session_id=session_id,
                created_at="",
                updated_at="",
                metadata_json=metadata or {},
            )
            identity = self._store.create_identity(identity)

        return identity

    def get_identity_by_session(self, channel_id: str, session_id: str) -> CommsIdentity | None:
        """Find the identity associated with a session on a specific channel."""
        identities = self._store.list_identities(channel_id=channel_id)
        return next((i for i in identities if i.session_id == session_id), None)
