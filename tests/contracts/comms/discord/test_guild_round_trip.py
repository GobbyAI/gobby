"""Operator-gated Discord test-guild round trip.

Discord ships only if this round trip passes against a real test guild; until an
operator names one, the adapter is deferred and this script is kept with the
provenance fixtures beside it. ``test_guild_round_trip`` skips unless
``DISCORD_TEST_GUILD_ID`` is set, and it checks that variable before it builds an
adapter or opens any socket, so validation runs never reach Discord.

To run it, an operator sets:

- ``DISCORD_TEST_GUILD_ID``: the test guild.
- ``DISCORD_TEST_CHANNEL_ID``: a text channel in that guild the bot can read and post in.
- ``DISCORD_TEST_USER_ID``: the test user who posts the inbound message.
- ``GOBBY_TEST_DISCORD_BOT_TOKEN``: the test bot's token, exported from the secret store
  for the run only.

and runs ``pytest tests/contracts/comms/discord/test_guild_round_trip.py -s``. The
stages run in order through the Python adapter: ``connect`` (the gateway reaches
READY), ``inbound`` (the test user posts in the channel while the script waits; the
adapter forwards that MESSAGE_CREATE), ``outbound`` (a REST send returns a message id),
``reconnect`` (the script drops the gateway socket and the adapter reconnects to RESUMED
or READY) and ``attachment`` (a multipart send of a small text file returns a message
id). A stage that raises is ``failed`` and every later stage is ``skipped``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import pytest

from gobby.communications.adapters import discord as discord_module
from gobby.communications.adapters.discord import DiscordAdapter
from gobby.communications.models import ChannelConfig, CommsAttachment, CommsMessage

logger = logging.getLogger(__name__)

STAGES = ("connect", "inbound", "outbound", "reconnect", "attachment")
GUILD_ENV = "DISCORD_TEST_GUILD_ID"
CHANNEL_ENV = "DISCORD_TEST_CHANNEL_ID"
USER_ENV = "DISCORD_TEST_USER_ID"
TOKEN_ENV = "GOBBY_TEST_DISCORD_BOT_TOKEN"
STAGE_TIMEOUT_SECONDS = 60.0
INBOUND_TIMEOUT_SECONDS = 300.0
CHANNEL_ROW_ID = "discord-test-guild"
RECONNECT_CLOSE_CODE = 4000

Outcome = Literal["passed", "failed", "skipped"]


class StageDriver(Protocol):
    """Runs one named stage; returning means the stage passed, raising means it failed."""

    def run_stage(self, stage: str) -> None: ...


class _ClosableGateway(Protocol):
    async def close(self, code: int = ..., reason: str = ...) -> None: ...


def run_round_trip(driver: StageDriver) -> dict[str, Outcome]:
    """Run every stage in order and report each as passed, failed or skipped."""
    outcomes: dict[str, Outcome] = {}
    failed = False
    for stage in STAGES:
        if failed:
            outcomes[stage] = "skipped"
            continue
        try:
            driver.run_stage(stage)
        except Exception:
            logger.exception("Discord round trip stage %s failed", stage)
            outcomes[stage] = "failed"
            failed = True
        else:
            outcomes[stage] = "passed"
    return outcomes


@dataclass(frozen=True)
class GuildSettings:
    guild_id: str
    channel_id: str
    user_id: str
    bot_token: str


def _settings_from_env(guild_id: str) -> GuildSettings:
    values = {name: os.environ.get(name, "") for name in (CHANNEL_ENV, USER_ENV, TOKEN_ENV)}
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        pytest.fail(f"{GUILD_ENV} is set but these variables are not: {missing}")
    return GuildSettings(
        guild_id=guild_id,
        channel_id=values[CHANNEL_ENV],
        user_id=values[USER_ENV],
        bot_token=values[TOKEN_ENV],
    )


class _WatchedAdapter(DiscordAdapter):
    """The real adapter, with its gateway socket and dispatches visible to the script."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatches: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.forwarded: list[CommsMessage] = []
        self.gateway: _ClosableGateway | None = None
        self.set_inbound_callback(self._record_forwarded)

    async def _record_forwarded(self, messages: list[CommsMessage]) -> list[CommsMessage]:
        self.forwarded.extend(messages)
        return messages

    async def _send_identify(self, ws: Any) -> None:
        self.gateway = ws
        await super()._send_identify(ws)

    async def _send_resume(self, ws: Any) -> None:
        self.gateway = ws
        await super()._send_resume(ws)

    async def _handle_gateway_dispatch(
        self, event_type: str | None, event_data: dict[str, Any]
    ) -> None:
        await super()._handle_gateway_dispatch(event_type, event_data)
        if event_type:
            self.dispatches.put_nowait((event_type, event_data))


class LiveGuildDriver:
    """Drives the Python Discord adapter against the operator's test guild."""

    def __init__(self, settings: GuildSettings, workdir: Path) -> None:
        self._settings = settings
        self._workdir = workdir
        self._runner = asyncio.Runner()
        self._adapter = _WatchedAdapter()
        self._stages: dict[str, Callable[[], Awaitable[None]]] = {
            "connect": self._connect,
            "inbound": self._inbound,
            "outbound": self._outbound,
            "reconnect": self._reconnect,
            "attachment": self._attachment,
        }

    def run_stage(self, stage: str) -> None:
        timeout = INBOUND_TIMEOUT_SECONDS if stage == "inbound" else STAGE_TIMEOUT_SECONDS
        self._runner.run(asyncio.wait_for(self._stages[stage](), timeout))

    def close(self) -> None:
        try:
            self._runner.run(self._adapter.shutdown())
        finally:
            self._runner.close()

    async def _wait_for_dispatch(self, wanted: set[str]) -> dict[str, Any]:
        while True:
            event_type, event_data = await self._adapter.dispatches.get()
            if event_type in wanted:
                return event_data

    def _message(self, content: str) -> CommsMessage:
        return CommsMessage(
            id=f"discord-round-trip-{uuid.uuid4().hex}",
            channel_id=CHANNEL_ROW_ID,
            direction="outbound",
            content=content,
            created_at=datetime.now(UTC),
            metadata_json={"platform_destination": self._settings.channel_id},
        )

    async def _connect(self) -> None:
        if not discord_module.HAS_WEBSOCKETS:
            raise RuntimeError("the websockets package is not installed; no gateway can run")
        now = datetime.now(UTC)
        config = ChannelConfig(
            id=CHANNEL_ROW_ID,
            channel_type="discord",
            name=CHANNEL_ROW_ID,
            enabled=True,
            config_json={"bot_token": self._settings.bot_token},
            created_at=now,
            updated_at=now,
        )
        await self._adapter.initialize(config, lambda _ref: None)
        await self._wait_for_dispatch({"READY"})

    async def _inbound(self) -> None:
        print(
            f"Post a message in channel {self._settings.channel_id} of guild "
            f"{self._settings.guild_id} as user {self._settings.user_id} now."
        )
        while True:
            data = await self._wait_for_dispatch({"MESSAGE_CREATE"})
            author = data.get("author", {})
            if (
                data.get("guild_id") == self._settings.guild_id
                and data.get("channel_id") == self._settings.channel_id
                and author.get("id") == self._settings.user_id
            ):
                break
        forwarded_ids = {message.platform_message_id for message in self._adapter.forwarded}
        if data.get("id") not in forwarded_ids:
            raise RuntimeError(f"MESSAGE_CREATE {data.get('id')} was not forwarded inbound")

    async def _outbound(self) -> None:
        message_id = await self._adapter.send_message(
            self._message("Gobby Discord round trip: outbound")
        )
        if not message_id:
            raise RuntimeError("the outbound send returned no message id")

    async def _reconnect(self) -> None:
        dropped = self._adapter.gateway
        if dropped is None:
            raise RuntimeError("no gateway socket is open to drop")
        await dropped.close(code=RECONNECT_CLOSE_CODE, reason="round trip reconnect")
        await self._wait_for_dispatch({"RESUMED", "READY"})
        if self._adapter.gateway is dropped:
            raise RuntimeError("the gateway answered on the dropped socket")

    async def _attachment(self) -> None:
        body = b"Gobby Discord round trip: attachment\n"
        path = self._workdir / "gobby-round-trip.txt"
        path.write_bytes(body)
        message = self._message("Gobby Discord round trip: attachment")
        attachment = CommsAttachment(
            id=f"discord-round-trip-file-{uuid.uuid4().hex}",
            message_id=message.id,
            filename=path.name,
            content_type="text/plain",
            size_bytes=len(body),
        )
        message_id = await self._adapter.send_attachment(message, attachment, path)
        if not message_id:
            raise RuntimeError("the attachment send returned no message id")


@pytest.mark.timeout(INBOUND_TIMEOUT_SECONDS + len(STAGES) * STAGE_TIMEOUT_SECONDS)
def test_guild_round_trip() -> None:
    guild_id = os.environ.get(GUILD_ENV, "")
    if not guild_id:
        pytest.skip(f"{GUILD_ENV} is not set; the Discord test-guild round trip is operator-gated")
    settings = _settings_from_env(guild_id)
    with tempfile.TemporaryDirectory() as workdir:
        driver = LiveGuildDriver(settings, Path(workdir))
        try:
            outcomes = run_round_trip(driver)
        finally:
            driver.close()
    assert outcomes == dict.fromkeys(STAGES, "passed"), f"Discord round trip: {outcomes}"
