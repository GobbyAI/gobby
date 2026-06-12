"""SMS channel adapter using Twilio REST API."""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

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


class SMSAdapter(BaseChannelAdapter):
    """Adapter for SMS via Twilio."""

    _OPT_OUT_KEYWORDS = frozenset({"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
    _OPT_IN_KEYWORDS = frozenset({"START", "UNSTOP", "YES"})

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._account_sid: str | None = None
        self._auth_token: str | None = None
        self._from_number: str | None = None
        self._messaging_service_sid: str | None = None
        self._webhook_url: str = ""

    @property
    def channel_type(self) -> str:
        """The unique type identifier for this channel."""
        return "sms"

    @property
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""
        return 1600

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
        auth_token_ref = config.config_json.get("auth_token", "")

        self._auth_token = secret_resolver(auth_token_ref) if auth_token_ref else None
        self._account_sid = config.config_json.get("account_sid")
        self._from_number = config.config_json.get("from_number")

        self._messaging_service_sid = config.config_json.get("messaging_service_sid")
        self._webhook_url = config.config_json.get("webhook_url", "")

        if not self._auth_token:
            raise ValueError("TWILIO_AUTH_TOKEN secret is required")
        if not self._account_sid:
            raise ValueError("account_sid is required in config_json")
        if not self._from_number and not self._messaging_service_sid:
            raise ValueError("from_number or messaging_service_sid is required in config_json")

        self._client = httpx.AsyncClient(
            base_url=f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/",
            auth=(self._account_sid, self._auth_token),
            timeout=30.0,
        )

        # In a real scenario we could make a request to test the auth, e.g. GET Messages.json
        # but that can be expensive/slow on Twilio. We just assume it's good.

    def _build_sender_payload(self) -> dict[str, str]:
        """Build the sender identification fields for Twilio API payloads."""
        if self._messaging_service_sid:
            return {"MessagingServiceSid": self._messaging_service_sid}
        if not self._from_number:
            raise ValueError("SMS adapter requires either messaging_service_sid or from_number")
        return {"From": self._from_number}

    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID."""
        if not self._client:
            raise RuntimeError("SMS adapter not initialized")
        client = self._client

        chunks = self.chunk_message(message.content, self.max_message_length)
        last_sid = None
        destination = self.platform_destination(message)

        for chunk in chunks:
            payload: dict[str, str] = {
                "To": destination,
                **self._build_sender_payload(),
                "Body": chunk,
            }

            response = await self._retry_request(
                functools.partial(client.post, "Messages.json", data=payload)
            )
            data = response.json()

            if "sid" not in data:
                raise ValueError(f"Failed to send SMS message, unknown error: {data}")

            last_sid = data["sid"]

        return last_sid

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send a file via MMS using Twilio MediaUrl parameter."""
        if not self._client:
            raise RuntimeError("SMS adapter not initialized")
        client = self._client

        if not attachment.platform_url:
            raise ValueError(
                "SMS/MMS attachments require a publicly accessible platform_url. "
                "Twilio fetches media from the URL directly."
            )

        payload: dict[str, Any] = {
            "To": self.platform_destination(message),
            **self._build_sender_payload(),
            "MediaUrl": attachment.platform_url,
        }
        if message.content:
            payload["Body"] = message.content

        response = await self._retry_request(
            functools.partial(client.post, "Messages.json", data=payload)
        )
        data = response.json()
        if "sid" not in data:
            raise ValueError(f"Failed to send MMS message: {data}")
        return str(data["sid"])

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        return ChannelCapabilities(
            threading=False,
            reactions=False,
            files=True,
            markdown=False,
            max_message_length=self.max_message_length,
        )

    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        """Normalize inbound webhook payload."""
        if isinstance(payload, bytes):
            # Twilio sends application/x-www-form-urlencoded
            payload_str = payload.decode("utf-8")
            params = dict(parse_qsl(payload_str, keep_blank_values=True))
        else:
            params = payload

        # For SMS, From is the sender number, To is our number, Body is the text
        from_number = params.get("From")
        body = str(params.get("Body") or "")
        message_sid = params.get("MessageSid")
        try:
            num_media = int(str(params.get("NumMedia", "0") or "0"))
        except ValueError:
            num_media = 0

        if not from_number or (not body and num_media <= 0):
            return []

        # Detect opt-out/opt-in keywords
        body_upper = body.strip().upper()
        opt_out_action: str | None = None
        if body_upper in self._OPT_OUT_KEYWORDS:
            opt_out_action = "opt_out"
        elif body_upper in self._OPT_IN_KEYWORDS:
            opt_out_action = "opt_in"

        metadata: dict[str, Any] = {
            **params,
            "opt_out_action": opt_out_action,
            "platform_channel_id": from_number,
            "external_username": from_number,
        }

        return [
            CommsMessage(
                id=message_sid or f"sms_{time.time()}",
                channel_id="",
                direction="inbound",
                content=body,
                created_at=datetime.now(UTC).isoformat(),
                identity_id=from_number,
                platform_message_id=message_sid,
                content_type="text",
                metadata_json=metadata,
            )
        ]

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook signature."""
        # Normalize headers to lowercase for reliable lookup
        lower_headers = {k.lower(): v for k, v in headers.items()}
        twilio_signature = lower_headers.get("x-twilio-signature")

        if not twilio_signature:
            return False

        # Twilio needs the exact URL for signature verification.
        # Priority: 1) config webhook_url, 2) x-original-url header, 3) x-gobby-webhook-url header
        url = (
            self._webhook_url
            or lower_headers.get("x-original-url")
            or lower_headers.get("x-gobby-webhook-url")
        )

        if not url:
            logger.warning(
                "SMS webhook verification failed: no webhook_url configured and no URL headers present. "
                "Set webhook_url in channel config for reliable signature verification."
            )
            return False

        # Parse the payload to sort the params
        payload_str = payload.decode("utf-8")
        params = dict(parse_qsl(payload_str, keep_blank_values=True))

        # Sort params and append to URL per Twilio's signature spec
        data = url
        for key in sorted(params.keys()):
            data += f"{key}{params[key]}"

        mac = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
        computed = base64.b64encode(mac.digest()).decode("utf-8")

        return hmac.compare_digest(computed, twilio_signature)


# Register the adapter
register_adapter("sms", SMSAdapter)
