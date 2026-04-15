"""Discord channel adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import random
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

HAS_WEBSOCKETS = False
try:
    import websockets

    HAS_WEBSOCKETS = True
except ImportError:
    pass

HAS_CRYPTOGRAPHY = False
try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    HAS_CRYPTOGRAPHY = True
except ImportError:
    pass


class DiscordAdapter(BaseChannelAdapter):
    """Adapter for Discord using Gateway for receiving and REST for sending."""

    _DEFAULT_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._gateway_task: asyncio.Task[Any] | None = None
        self._bot_token: str = ""
        self._gateway_url: str = DiscordAdapter._DEFAULT_GATEWAY_URL
        # Gateway session state for RESUME
        self._session_id: str | None = None
        self._resume_gateway_url: str | None = None
        self._sequence: int | None = None
        # Per-route REST rate limit tracking
        self._route_buckets: dict[str, dict[str, Any]] = {}

    @property
    def channel_type(self) -> str:
        """The unique type identifier for this channel."""
        return "discord"

    @property
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""
        return 2000

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
        token_ref = config.config_json.get("bot_token", "$secret:DISCORD_BOT_TOKEN")
        token = secret_resolver(token_ref) if token_ref.startswith("$secret:") else token_ref

        if not token:
            raise ValueError(f"Could not resolve Discord bot token: {token_ref}")

        self._bot_token = token

        # Set up REST API client
        self._client = httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            headers={"Authorization": f"Bot {self._bot_token}"},
            timeout=30.0,
        )

        # Fetch dynamic gateway URL from Discord API
        await self._fetch_gateway_url()

        if HAS_WEBSOCKETS and config.config_json.get("enable_gateway", True):
            self._gateway_task = asyncio.create_task(self._run_gateway())

    async def _fetch_gateway_url(self) -> None:
        """Fetch gateway URL from GET /gateway/bot with graceful fallback."""
        if not self._client:
            return

        try:
            response = await self._client.get("/gateway/bot")
            if response.status_code == 200:
                data = response.json()
                url = data.get("url")
                if url:
                    self._gateway_url = f"{url}?v=10&encoding=json"
                    logger.info("Discord gateway URL fetched: %s", self._gateway_url)

                session_limit = data.get("session_start_limit", {})
                remaining = session_limit.get("remaining", "?")
                total = session_limit.get("total", "?")
                logger.info("Discord session start limit: %s/%s remaining", remaining, total)
            else:
                logger.warning(
                    "Failed to fetch Discord gateway URL (HTTP %d), using default",
                    response.status_code,
                )
        except Exception as e:
            logger.warning("Failed to fetch Discord gateway URL: %s, using default", e)

    async def _run_gateway(self) -> None:
        """Background task to connect to Discord Gateway and handle events."""
        if not HAS_WEBSOCKETS:
            return

        try:
            backoff = 1.0
            max_backoff = 120.0
            retry_count = 0
            while True:
                try:
                    gateway_url = self._resume_gateway_url or self._gateway_url
                    async with websockets.connect(gateway_url) as ws:
                        # Attempt RESUME if we have a prior session
                        if self._session_id and self._sequence is not None:
                            resume_payload = {
                                "op": 6,
                                "d": {
                                    "token": self._bot_token,
                                    "session_id": self._session_id,
                                    "seq": self._sequence,
                                },
                            }
                            await ws.send(json.dumps(resume_payload))
                            logger.info(
                                "Discord gateway: sent RESUME (session=%s)", self._session_id
                            )
                        else:
                            await self._send_identify(ws)

                        heartbeat_interval: float | None = None
                        heartbeat_task: asyncio.Task[Any] | None = None

                        try:
                            async for raw_message in ws:
                                if not isinstance(raw_message, (str, bytes)):
                                    continue
                                data = json.loads(raw_message)
                                op = data.get("op")

                                # Track sequence number from all dispatches
                                if data.get("s") is not None:
                                    self._sequence = data["s"]

                                if op == 10:  # Hello — start heartbeating
                                    heartbeat_interval = data["d"]["heartbeat_interval"] / 1000.0
                                    if heartbeat_task and not heartbeat_task.done():
                                        heartbeat_task.cancel()
                                    heartbeat_task = asyncio.create_task(
                                        self._heartbeat_loop(ws, heartbeat_interval)
                                    )

                                elif op == 11:  # Heartbeat ACK
                                    pass  # Acknowledged

                                elif op == 9:  # Invalid Session
                                    resumable = data.get("d", False)
                                    if not resumable:
                                        logger.warning(
                                            "Discord gateway: invalid session (not resumable), re-identifying"
                                        )
                                        self._session_id = None
                                        self._resume_gateway_url = None
                                        self._sequence = None
                                        await asyncio.sleep(1 + 4 * random.random())  # nosec B311 # jitter, not crypto
                                        await self._send_identify(ws)
                                    else:
                                        logger.info(
                                            "Discord gateway: invalid session (resumable), re-sending RESUME"
                                        )
                                        await asyncio.sleep(1 + 4 * random.random())  # nosec B311 # jitter, not crypto
                                        await ws.send(
                                            json.dumps(
                                                {
                                                    "op": 6,
                                                    "d": {
                                                        "token": self._bot_token,
                                                        "session_id": self._session_id,
                                                        "seq": self._sequence,
                                                    },
                                                }
                                            )
                                        )

                                elif op == 0:  # Dispatch
                                    event_type = data.get("t")

                                    if event_type == "READY":
                                        d = data.get("d", {})
                                        self._session_id = d.get("session_id")
                                        self._resume_gateway_url = d.get("resume_gateway_url")
                                        backoff = 1.0
                                        retry_count = 0
                                        logger.info(
                                            "Discord gateway: READY (session=%s)",
                                            self._session_id,
                                        )

                                    elif event_type == "RESUMED":
                                        backoff = 1.0
                                        retry_count = 0
                                        logger.info("Discord gateway: RESUMED successfully")

                                    elif event_type == "MESSAGE_CREATE":
                                        msg_data = data.get("d", {})
                                        logger.debug(
                                            "Discord gateway received MESSAGE_CREATE: %s",
                                            msg_data.get("id"),
                                        )
                        finally:
                            if heartbeat_task and not heartbeat_task.done():
                                heartbeat_task.cancel()

                except Exception as e:
                    retry_count += 1
                    jitter = random.uniform(0, backoff * 0.5)  # nosec B311
                    delay = min(backoff + jitter, max_backoff)
                    logger.warning(
                        "Discord gateway connection error (attempt %d, retry in %.1fs): %s",
                        retry_count,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
                    backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            pass

    async def _send_identify(self, ws: Any) -> None:
        """Send IDENTIFY payload to the gateway."""
        identify_payload = {
            "op": 2,
            "d": {
                "token": self._bot_token,
                "intents": 37376,
                "properties": {
                    "os": "linux",
                    "browser": "gobby",
                    "device": "gobby",
                },
            },
        }
        await ws.send(json.dumps(identify_payload))
        logger.info("Discord gateway: sent IDENTIFY")

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        """Send periodic heartbeats to keep the gateway connection alive."""
        try:
            # Discord expects a jittered first heartbeat (random fraction of interval)
            await asyncio.sleep(random.random() * interval)  # nosec B311 # jitter, not crypto
            await ws.send(json.dumps({"op": 1, "d": self._sequence}))
            while True:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": self._sequence}))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Discord heartbeat error: %s", e)

    async def _rate_limited_request(
        self, route: str, method: str = "post", **kwargs: Any
    ) -> httpx.Response:
        """Make a REST API request with per-route rate limit tracking.

        Checks the local rate limit bucket before making the request,
        sleeps if the bucket is exhausted, then updates the bucket from
        response headers.
        """
        if not self._client:
            raise RuntimeError("Discord adapter not initialized")

        # Pre-request: check if route bucket is exhausted
        bucket = self._route_buckets.get(route)
        if bucket and bucket["remaining"] == 0:
            wait = bucket["reset"] - time.time()
            if wait > 0:
                logger.debug("Discord REST: rate limit pre-wait %.1fs for %s", wait, route)
                await asyncio.sleep(wait)

        client = self._client
        response = await self._retry_request(lambda: getattr(client, method)(route, **kwargs))

        # Post-request: parse rate limit headers
        headers = response.headers
        if "X-RateLimit-Remaining" in headers:
            try:
                remaining = int(headers["X-RateLimit-Remaining"])
            except (ValueError, TypeError):
                remaining = 0
            try:
                reset = float(headers.get("X-RateLimit-Reset", "0"))
            except (ValueError, TypeError):
                reset = 0.0
            self._route_buckets[route] = {
                "remaining": remaining,
                "reset": reset,
                "bucket_id": headers.get("X-RateLimit-Bucket", ""),
            }

        return response

    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID.

        Supports content types:
        - 'text' (default): plain text, chunked if needed
        - 'embed': Discord embed JSON (dict or list of dicts)
        - 'markdown': sent as plain content (Discord natively supports markdown)
        """
        if not self._client:
            raise RuntimeError("Discord adapter not initialized")

        if message.content_type == "embed":
            return await self._send_embed(message)
        else:
            return await self._send_text(message)

    async def _send_text(self, message: CommsMessage) -> str | None:
        """Send a plain text message, chunked if needed."""
        chunks = self.chunk_message(message.content, self.max_message_length)
        last_id = None
        channel_id = message.platform_thread_id or message.channel_id

        for chunk in chunks:
            payload: dict[str, Any] = {"content": chunk}
            route = f"/channels/{channel_id}/messages"
            response = await self._rate_limited_request(route, "post", json=payload)
            data = response.json()
            last_id = data.get("id")

        return last_id

    async def _send_embed(self, message: CommsMessage) -> str | None:
        """Send a Discord embed message (no chunking)."""
        try:
            embed_data = json.loads(message.content)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"Invalid embed JSON: {e}") from e

        if isinstance(embed_data, dict):
            embed_data = [embed_data]
        if not isinstance(embed_data, list):
            raise ValueError("Embed content must be a JSON object or array of embed objects")

        # Validate embed limits
        for i, embed in enumerate(embed_data):
            if not isinstance(embed, dict):
                raise ValueError(f"Embed at index {i} must be a JSON object")
            title = embed.get("title", "")
            if len(title) > 256:
                raise ValueError(f"Embed title exceeds 256 chars (got {len(title)})")
            desc = embed.get("description", "")
            if len(desc) > 4096:
                raise ValueError(f"Embed description exceeds 4096 chars (got {len(desc)})")
            fields = embed.get("fields", [])
            if len(fields) > 25:
                raise ValueError(f"Embed has {len(fields)} fields (max 25)")

        channel_id = message.platform_thread_id or message.channel_id
        payload: dict[str, Any] = {"embeds": embed_data}
        fallback = message.metadata_json.get("fallback_text")
        if fallback:
            payload["content"] = fallback

        route = f"/channels/{channel_id}/messages"
        response = await self._rate_limited_request(route, "post", json=payload)
        data = response.json()
        result: str | None = data.get("id")
        return result

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send a file via Discord multipart form data."""
        if not self._client:
            raise RuntimeError("Discord adapter not initialized")

        channel_id = message.platform_thread_id or message.channel_id
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
        data: dict[str, Any] = {}
        if message.content:
            data["content"] = message.content
        payload_json = json.dumps(data) if data else json.dumps({})
        files: dict[str, Any] = {
            "files[0]": (attachment.filename, file_bytes, attachment.content_type),
        }
        route = f"/channels/{channel_id}/messages"
        response = await self._rate_limited_request(
            route, "post", data={"payload_json": payload_json}, files=files
        )
        result = response.json()
        msg_id: str | None = result.get("id")
        return msg_id

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._gateway_task:
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except asyncio.CancelledError:
                pass
            self._gateway_task = None

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

        # Discord Interactions structure
        messages: list[CommsMessage] = []

        # Interaction type 1 is PING
        if payload_dict.get("type") == 1:
            # The router should return {"type": 1} to acknowledge the ping
            # But the adapter API doesn't have a way to respond with data,
            # so we'd normally just return empty and let router handle ping
            return messages

        # Handle MESSAGE_REACTION_ADD events
        event_type = payload_dict.get("t")
        if event_type == "MESSAGE_REACTION_ADD":
            d = payload_dict.get("d", {})
            user_id = d.get("user_id")
            channel_id = d.get("channel_id")
            msg_id = d.get("message_id")
            emoji = d.get("emoji", {})
            reaction = emoji.get("name", "")

            if channel_id and reaction and msg_id:
                messages.append(
                    CommsMessage(
                        id=f"discord_rxn_{msg_id}_{time.time()}",
                        channel_id=channel_id,
                        direction="inbound",
                        content=reaction,
                        created_at=datetime.now(UTC).isoformat(),
                        identity_id=user_id,
                        platform_message_id=msg_id,
                        content_type="reaction",
                        metadata_json=d,
                    )
                )
            return messages

        # Interaction type or MESSAGE_CREATE structure
        data = payload_dict.get("data", {})

        # Fallback to direct MESSAGE_CREATE payload (gateway-like but via webhook if applicable)
        msg_data = payload_dict if "content" in payload_dict else data

        content = msg_data.get("content", "")
        author = payload_dict.get("member", {}).get("user", {}) or payload_dict.get("author", {})
        user_id = author.get("id")
        channel_id = payload_dict.get("channel_id")
        msg_id = payload_dict.get("id") or msg_data.get("id")
        # Extract thread ID from message reference or thread metadata
        thread_id = None
        message_reference = payload_dict.get("message_reference")
        if message_reference:
            thread_id = message_reference.get("channel_id")
        thread_meta = payload_dict.get("thread")
        if thread_meta:
            thread_id = thread_meta.get("id")

        if channel_id and content:
            messages.append(
                CommsMessage(
                    id=msg_id or f"discord_msg_{time.time()}",
                    channel_id=channel_id,
                    direction="inbound",
                    content=content,
                    created_at=datetime.now(UTC).isoformat(),
                    identity_id=user_id,
                    platform_message_id=msg_id,
                    platform_thread_id=thread_id,
                    content_type="text",
                    metadata_json=payload_dict,
                )
            )

        return messages

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook signature."""
        if not HAS_CRYPTOGRAPHY:
            logger.warning("cryptography package missing, cannot verify Discord webhook signature")
            return False

        signature = headers.get("X-Signature-Ed25519") or headers.get("x-signature-ed25519")
        timestamp = headers.get("X-Signature-Timestamp") or headers.get("x-signature-timestamp")

        if not signature or not timestamp or not secret:
            return False

        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(secret))
            public_key.verify(bytes.fromhex(signature), timestamp.encode() + payload)
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False


# Register the adapter
register_adapter("discord", DiscordAdapter)
