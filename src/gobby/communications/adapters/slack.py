"""Slack channel adapter."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from gobby.communications.adapters import register_adapter
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.models import (
    ChannelCapabilities,
    ChannelConfig,
    CommsAttachment,
    CommsMessage,
)

logger = logging.getLogger(__name__)


class SlackAdapter(BaseChannelAdapter):
    """Adapter for Slack Web API and Events API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._bot_token: str | None = None
        self._signing_secret: str | None = None
        self._bot_user_id: str | None = None

    @property
    def channel_type(self) -> str:
        """The unique type identifier for this channel."""
        return "slack"

    @property
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""
        return 3000

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
        bot_token_ref = config.config_json.get("bot_token", "")
        signing_secret_ref = config.config_json.get("signing_secret", "")

        self._bot_token = secret_resolver(bot_token_ref) if bot_token_ref else None
        self._signing_secret = secret_resolver(signing_secret_ref) if signing_secret_ref else None

        if not self._bot_token:
            raise ValueError("SLACK_BOT_TOKEN secret is required")

        # signing secret is optional if not using webhooks, but usually required
        if not self._signing_secret:
            logger.warning("SLACK_SIGNING_SECRET secret is not set, webhook verification will fail")

        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api/",
            headers={"Authorization": f"Bearer {self._bot_token}"},
            timeout=30.0,
        )

        # Verify token via auth.test
        response = await self._client.post("auth.test")
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise ValueError(f"Slack auth.test failed: {data.get('error')}")

        self._bot_user_id = data.get("user_id")

    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID.

        Supports three content types:
        - 'text' (default): plain text message
        - 'blocks': Block Kit JSON array (message.content is JSON string)
        - 'markdown': wraps content in a mrkdwn section block
        """
        if not self._client:
            raise RuntimeError("Slack adapter not initialized")
        client = self._client

        if message.content_type == "blocks":
            return await self._send_blocks(client, message)
        elif message.content_type == "markdown":
            return await self._send_markdown(client, message)
        else:
            return await self._send_text(client, message)

    async def _send_text(self, client: httpx.AsyncClient, message: CommsMessage) -> str | None:
        """Send a plain text message, chunked if needed."""
        chunks = self.chunk_message(message.content, self.max_message_length)
        last_ts = None
        channel = self.platform_destination(message)

        for chunk in chunks:
            payload: dict[str, Any] = {
                "channel": channel,
                "text": chunk,
            }
            if message.platform_thread_id:
                payload["thread_ts"] = message.platform_thread_id

            response = await self._retry_request(
                functools.partial(client.post, "chat.postMessage", json=payload)
            )
            data = response.json()
            if not data.get("ok"):
                error_msg = data.get("error", "Unknown error")
                raise ValueError(f"Failed to send Slack message: {error_msg}")

            last_ts = data.get("ts")

        return last_ts

    async def _send_blocks(self, client: httpx.AsyncClient, message: CommsMessage) -> str | None:
        """Send a Block Kit message (no chunking)."""
        try:
            blocks = json.loads(message.content)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"Invalid Block Kit JSON: {e}") from e

        if not isinstance(blocks, list):
            raise ValueError("Block Kit content must be a JSON array of blocks")
        for i, block in enumerate(blocks):
            if not isinstance(block, dict) or "type" not in block:
                raise ValueError(f"Block at index {i} must be a dict with a 'type' field")

        fallback = message.metadata_json.get("fallback_text", "Message from Gobby")
        payload: dict[str, Any] = {
            "channel": self.platform_destination(message),
            "blocks": blocks,
            "text": fallback,
        }
        if message.platform_thread_id:
            payload["thread_ts"] = message.platform_thread_id

        response = await self._retry_request(
            functools.partial(client.post, "chat.postMessage", json=payload)
        )
        data = response.json()
        if not data.get("ok"):
            raise ValueError(f"Failed to send Slack message: {data.get('error', 'Unknown error')}")

        result: str | None = data.get("ts")
        return result

    async def _send_markdown(self, client: httpx.AsyncClient, message: CommsMessage) -> str | None:
        """Send markdown content wrapped in a mrkdwn section block."""
        chunks = self.chunk_message(message.content, self.max_message_length)
        last_ts = None
        channel = self.platform_destination(message)

        for chunk in chunks:
            payload: dict[str, Any] = {
                "channel": channel,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}],
                "text": chunk,
            }
            if message.platform_thread_id:
                payload["thread_ts"] = message.platform_thread_id

            response = await self._retry_request(
                functools.partial(client.post, "chat.postMessage", json=payload)
            )
            data = response.json()
            if not data.get("ok"):
                raise ValueError(
                    f"Failed to send Slack message: {data.get('error', 'Unknown error')}"
                )

            last_ts = data.get("ts")

        return last_ts

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send a file via Slack's files.getUploadURLExternal 3-step flow."""
        if not self._client:
            raise RuntimeError("Slack adapter not initialized")
        client = self._client

        file_bytes = await asyncio.to_thread(file_path.read_bytes)

        # Step 1: Get upload URL
        get_url_data: dict[str, Any] = {
            "filename": attachment.filename,
            "length": len(file_bytes),
        }
        response = await self._retry_request(
            functools.partial(client.post, "files.getUploadURLExternal", data=get_url_data)
        )
        result = response.json()
        if not result.get("ok"):
            raise ValueError(f"files.getUploadURLExternal failed: {result.get('error')}")
        upload_url = result["upload_url"]
        file_id = result["file_id"]

        # Step 2: PUT file bytes to the upload URL (with retry)
        async with httpx.AsyncClient(timeout=60.0) as upload_client:
            upload_resp = await self._retry_request(
                functools.partial(
                    upload_client.put,
                    upload_url,
                    content=file_bytes,
                    headers={"Content-Type": attachment.content_type},
                )
            )
            if upload_resp.status_code >= 400:
                raise ValueError(
                    f"Slack file upload PUT failed with status {upload_resp.status_code}"
                )

        # Step 3: Complete the upload
        complete_payload: dict[str, Any] = {
            "files": json.dumps([{"id": file_id, "title": attachment.filename}]),
            "channel_id": self.platform_destination(message),
        }
        if message.platform_thread_id:
            complete_payload["thread_ts"] = message.platform_thread_id
        if message.content:
            complete_payload["initial_comment"] = message.content

        response = await self._retry_request(
            functools.partial(client.post, "files.completeUploadExternal", data=complete_payload)
        )
        result = response.json()
        if not result.get("ok"):
            raise ValueError(f"files.completeUploadExternal failed: {result.get('error')}")

        # Extract message ts from the completed upload
        files_list = result.get("files", [])
        if files_list:
            shares = files_list[0].get("shares", {})
            for channel_shares in shares.values():
                for share_list in channel_shares.values():
                    if share_list:
                        return str(share_list[0].get("ts"))
        logger.warning("Failed to extract uploaded message ts from Slack response: %s", result)
        return None

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        return ChannelCapabilities(
            threading=True,
            reactions=True,
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

        # Handle url_verification
        if payload_dict.get("type") == "url_verification":
            challenge = payload_dict.get("challenge", "")
            return [
                CommsMessage(
                    id=f"slack_url_verification_{time.time()}",
                    channel_id="",
                    direction="inbound",
                    content=challenge,
                    created_at=datetime.now(UTC).isoformat(),
                    content_type="url_verification",
                    metadata_json=payload_dict,
                )
            ]

        messages: list[CommsMessage] = []

        if payload_dict.get("type") == "event_callback":
            event = payload_dict.get("event", {})
            event_type = event.get("type")

            # Only process message events, ignore bot messages and edits for now
            if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
                text = event.get("text", "")
                user = event.get("user")
                channel = event.get("channel")
                ts = event.get("ts")
                thread_ts = event.get("thread_ts")
                external_username = event.get("username") or event.get("user_name") or user

                if channel and text and user:
                    metadata = {
                        **event,
                        "platform_channel_id": channel,
                        "external_username": external_username,
                    }
                    messages.append(
                        CommsMessage(
                            id=ts or f"slack_msg_{time.time()}",
                            channel_id="",
                            direction="inbound",
                            content=text,
                            created_at=datetime.now(UTC).isoformat(),
                            identity_id=user,
                            platform_message_id=ts,
                            platform_thread_id=thread_ts,
                            content_type="text",
                            metadata_json=metadata,
                        )
                    )
            elif event_type == "reaction_added":
                user = event.get("user")
                reaction = event.get("reaction")
                item = event.get("item", {})
                external_username = event.get("username") or event.get("user_name") or user

                if item.get("type") == "message":
                    channel = item.get("channel")
                    ts = item.get("ts")

                    if channel and reaction and ts and user:
                        metadata = {
                            **event,
                            "platform_channel_id": channel,
                            "external_username": external_username,
                        }
                        messages.append(
                            CommsMessage(
                                id=f"slack_rxn_{ts}_{time.time()}",
                                channel_id="",
                                direction="inbound",
                                content=reaction,
                                created_at=datetime.now(UTC).isoformat(),
                                identity_id=user,
                                platform_message_id=ts,  # ID of message being reacted to
                                content_type="reaction",
                                metadata_json=metadata,
                            )
                        )

        return messages

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook signature."""
        timestamp = headers.get("x-slack-request-timestamp")
        slack_signature = headers.get("x-slack-signature")

        if not timestamp or not slack_signature:
            # Try lowercase variations if necessary, though ASGI/WSGI usually normalizes to lower
            timestamp = headers.get("X-Slack-Request-Timestamp") or timestamp
            slack_signature = headers.get("X-Slack-Signature") or slack_signature

        if not timestamp or not slack_signature:
            return False

        # Prevent replay attacks
        try:
            if abs(time.time() - float(timestamp)) > 60 * 5:
                return False
        except ValueError:
            return False

        sig_basestring = f"v0:{timestamp}:{payload.decode('utf-8')}"
        my_signature = (
            "v0="
            + hmac.new(
                secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        )

        return hmac.compare_digest(my_signature, slack_signature)


# Register the adapter
register_adapter("slack", SlackAdapter)
