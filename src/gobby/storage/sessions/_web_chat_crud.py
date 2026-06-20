"""Web-chat CRUD helpers for session storage."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from gobby.storage.hub.protocol import HubDatabase, WebChatSessionBootstrap
from gobby.storage.session_models import Session


class _SessionWebChatCRUDHost(Protocol):
    db: HubDatabase
    _VALID_CHAT_MODES: ClassVar[set[str]]

    def register(
        self,
        *,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str,
        title: str | None = None,
        session_type: str = "terminal",
        is_local: bool = False,
        sandbox_enabled: bool | None = None,
        sandbox_policy_hash: str | None = None,
    ) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...


class _SessionWebChatCRUDMixin:
    def create_web_chat_session(
        self: _SessionWebChatCRUDHost,
        *,
        machine_id: str,
        project_id: str,
        source: str,
        title: str | None = None,
        model: str | None = None,
        is_local: bool = False,
        chat_mode: str | None = None,
        sandbox_enabled: bool,
        sandbox_policy_hash: str,
    ) -> Session:
        """Create a new web-chat session with a temporary runtime identity."""
        if chat_mode is not None and chat_mode not in self._VALID_CHAT_MODES:
            modes = ", ".join(sorted(self._VALID_CHAT_MODES))
            raise ValueError(f"Invalid chat_mode {chat_mode!r}. Must be one of: {modes}")

        bootstrap_external_id = f"web-chat-bootstrap:{uuid.uuid4()}"
        with self.db.transaction_immediate(
            WebChatSessionBootstrap(
                external_id=bootstrap_external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                session_type="web_chat",
            )
        ):
            session = self.register(
                external_id=bootstrap_external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                title=title,
                session_type="web_chat",
                is_local=is_local,
                sandbox_enabled=sandbox_enabled,
                sandbox_policy_hash=sandbox_policy_hash,
            )
            if model is None and chat_mode is None:
                return session

            now = datetime.now(UTC).isoformat()
            self.db.execute(
                """
                UPDATE sessions
                SET model = COALESCE(%s, model),
                    is_local = %s,
                    chat_mode = COALESCE(%s, chat_mode),
                    updated_at = %s
                WHERE id = %s
                """,
                (model, bool(is_local), chat_mode, now, session.id),
            )
            updated = self.get(session.id)
            if updated is None:
                raise RuntimeError(f"Web chat session {session.id} disappeared after update")
            return updated
