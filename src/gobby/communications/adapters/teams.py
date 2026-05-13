import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from gobby.communications.adapters import register_adapter
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.models import ChannelCapabilities, ChannelConfig, CommsMessage

logger = logging.getLogger(__name__)


class TeamsAdapter(BaseChannelAdapter):
    """Microsoft Teams Bot Framework adapter."""

    _BOTFRAMEWORK_JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
    _BOTFRAMEWORK_ISSUER = "https://api.botframework.com"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._app_id: str = ""
        self._app_password: str = ""
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._token_lock = asyncio.Lock()
        self._jwk_client: PyJWKClient | None = None
        # Cache of ConversationReferences keyed by conversation_id
        self._conversation_refs: dict[str, dict[str, Any]] = {}

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ValueError("Adapter not initialized")
        return self._client

    @property
    def channel_type(self) -> str:
        """The unique type identifier for this channel."""
        return "teams"

    @property
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""
        return 28000

    @property
    def supports_webhooks(self) -> bool:
        """Whether this adapter supports inbound webhooks."""
        return True

    @property
    def supports_polling(self) -> bool:
        """Whether this adapter supports message polling."""
        return False

    async def initialize(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Set up API clients, validate credentials."""
        self._app_id = secret_resolver("$secret:TEAMS_APP_ID") or ""
        self._app_password = secret_resolver("$secret:TEAMS_APP_PASSWORD") or ""

        if not self._app_id or not self._app_password:
            raise ValueError("TEAMS_APP_ID and TEAMS_APP_PASSWORD secrets are required")

        self._client = httpx.AsyncClient(timeout=30.0)
        await self._refresh_token()

    async def _refresh_token(self) -> None:
        """Re-obtain OAuth access token when expired."""
        if not self._client:
            return

        token_url = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_password,
            "scope": "https://api.botframework.com/.default",
        }

        response = await self._client.post(token_url, data=data)
        response.raise_for_status()

        token_data = response.json()
        self._access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 3600)
        self._token_expires_at = time.time() + expires_in - 300  # Refresh 5 mins early

    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID."""
        client = self._require_client()

        async with self._token_lock:
            if time.time() >= self._token_expires_at:
                await self._refresh_token()

        service_url = message.metadata_json.get("service_url")
        if not service_url:
            raise ValueError("Missing service_url in message metadata")

        conversation_id = message.channel_id
        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"

        activity: dict[str, Any] = {
            "type": "message",
            "text": message.content,
        }

        if message.content_type == "adaptive_card":
            try:
                card_content = json.loads(message.content)
                if not isinstance(card_content, dict):
                    raise ValueError(
                        f"Adaptive card content must be a JSON object, got {type(card_content).__name__}"
                    )
            except json.JSONDecodeError as exc:
                raise ValueError("Adaptive card content is not valid JSON") from exc
            activity["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card_content,
                }
            ]
            activity["text"] = ""

        if message.platform_thread_id:
            activity["replyToId"] = message.platform_thread_id

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        response = await self._retry_request(
            lambda: client.post(url, json=activity, headers=headers)
        )

        data = response.json()
        message_id = data.get("id")
        return str(message_id) if message_id else None

    async def send_proactive(
        self, conversation_id: str, content: str, content_type: str = "text"
    ) -> str | None:
        """Send a proactive message to a Teams conversation using stored ConversationReference.

        Args:
            conversation_id: The Teams conversation ID to message.
            content: Message content.
            content_type: Content type ('text' or 'adaptive_card').

        Returns:
            Platform message ID if successful, None otherwise.

        Raises:
            ValueError: If no ConversationReference is stored for the conversation.
        """
        client = self._require_client()

        conv_ref = self._conversation_refs.get(conversation_id)
        if not conv_ref:
            raise ValueError(
                f"No ConversationReference stored for conversation {conversation_id}. "
                "A prior inbound message from this conversation is required."
            )

        async with self._token_lock:
            if time.time() >= self._token_expires_at:
                await self._refresh_token()

        service_url = conv_ref["service_url"]
        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"

        activity: dict[str, Any] = {
            "type": "message",
            "text": content,
        }

        if content_type == "adaptive_card":
            try:
                card_content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError("Adaptive card content is not valid JSON") from exc
            activity["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card_content,
                }
            ]
            activity["text"] = ""

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        response = await self._retry_request(
            lambda: client.post(url, json=activity, headers=headers)
        )

        data = response.json()
        message_id = data.get("id")
        return str(message_id) if message_id else None

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        return ChannelCapabilities(
            threading=True,
            reactions=False,
            files=True,
            markdown=True,
            max_message_length=self.max_message_length,
        )

    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        """Normalize inbound webhook payload."""
        if isinstance(payload, bytes):
            try:
                payload_dict = json.loads(payload)
            except json.JSONDecodeError as e:
                raise ValueError("Invalid JSON payload") from e
        else:
            payload_dict = payload

        if payload_dict.get("type") != "message":
            return []

        from_user = payload_dict.get("from", {})
        conversation = payload_dict.get("conversation", {})

        identity_id = from_user.get("id")
        channel_id = conversation.get("id")
        text = payload_dict.get("text", "")
        message_id = payload_dict.get("id")
        reply_to_id = payload_dict.get("replyToId")
        service_url = payload_dict.get("serviceUrl")

        if not channel_id or not text:
            return []

        metadata: dict[str, Any] = {}
        if service_url:
            metadata["service_url"] = service_url

        # Store ConversationReference for proactive messaging
        conv_ref = {
            "service_url": service_url,
            "conversation_id": channel_id,
            "tenant_id": conversation.get("tenantId"),
            "bot_id": payload_dict.get("recipient", {}).get("id"),
        }
        metadata["conversation_reference"] = conv_ref
        if channel_id:
            self._conversation_refs[channel_id] = conv_ref

        msg = CommsMessage(
            id=message_id or "",
            channel_id=channel_id,
            direction="inbound",
            content=text,
            created_at=datetime.now(UTC).isoformat(),
            identity_id=identity_id,
            platform_message_id=message_id,
            platform_thread_id=reply_to_id,
            metadata_json=metadata,
        )

        return [msg]

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook JWT signature against Bot Framework JWKS."""
        auth_header = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth_header = v
                break

        if not auth_header.lower().startswith("bearer "):
            return False

        token = auth_header[7:]

        try:
            if self._jwk_client is None:
                self._jwk_client = PyJWKClient(self._BOTFRAMEWORK_JWKS_URL)

            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._app_id,
                issuer=self._BOTFRAMEWORK_ISSUER,
            )

            # Additional serviceUrl claim validation if present
            if "serviceUrl" in decoded and not decoded["serviceUrl"].startswith("https://"):
                return False

            return True
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as e:
            logger.debug("Teams webhook JWT verification failed: %s", e)
            return False


register_adapter("teams", TeamsAdapter)
