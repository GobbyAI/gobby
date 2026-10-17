"""Python access to the front door's authenticated node-command route."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from gobby.config.bootstrap import load_bootstrap
from gobby.hub.node_commands import ErrReply, NodeModel, OkReply, parse_message
from gobby.runner_backend import front_door_secret

type DeliveryStage = Literal["none", "partial"] | None


class NodeCommandError(Exception):
    """A routing refusal, node error, or uncertain delivery; never a fallback signal."""

    def __init__(self, code: str, message: str = "", stage: DeliveryStage = None) -> None:
        self.code = code
        self.stage = stage
        super().__init__(message or code)


class _ConnectedNodes(NodeModel):
    machine_ids: list[str]


class NodeCommandClient:
    """Send commands via the local front door without retrying uncertain writes."""

    def __init__(
        self,
        *,
        port: int,
        secret: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.port = port
        self.secret = secret
        self._transport = transport

    @classmethod
    def from_environment(cls) -> NodeCommandClient:
        return cls(port=load_bootstrap().daemon_port, secret=front_door_secret())

    def _http_client(self, read_timeout: float) -> httpx.AsyncClient:
        if not self.secret:
            raise NodeCommandError(
                "node_routing_unavailable", "Front door secret unavailable", "none"
            )
        return httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"X-Gobby-Backend-Secret": self.secret},
            timeout=httpx.Timeout(read_timeout, connect=5.0),
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def send(
        self, machine_id: str, command: str, payload: dict[str, Any], timeout_ms: int = 30000
    ) -> dict[str, Any]:
        """Return a validated result, preserving the node's error and delivery stage."""
        try:
            async with self._http_client(timeout_ms / 1000 + 5) as client:
                response = await client.post(
                    f"/api/internal/nodes/{quote(machine_id, safe='')}/commands",
                    json={"command": command, "payload": payload, "timeout_ms": timeout_ms},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NodeCommandError(
                "node_routing_unavailable", "Cannot connect to front door", "none"
            ) from exc
        except httpx.HTTPError as exc:
            raise NodeCommandError("node_reply_lost", "Node reply unavailable", None) from exc
        if response.status_code == 404:
            raise NodeCommandError("node_routing_unavailable", "Node routing unavailable", "none")
        if response.status_code != 200:
            raise NodeCommandError("node_reply_lost", "Front door did not return a reply", None)
        try:
            reply = parse_message("reply", response.text)
        except ValidationError as exc:
            raise NodeCommandError("node_reply_lost", "Invalid node reply", None) from exc
        if isinstance(reply, ErrReply):
            raise NodeCommandError(reply.error.code, reply.error.message, reply.error.stage)
        assert isinstance(reply, OkReply)
        return reply.result.model_dump(mode="json")

    async def connected_machine_ids(self) -> set[str]:
        """Read channel availability; a failed read never implies an empty fleet."""
        try:
            async with self._http_client(5.0) as client:
                response = await client.get("/api/internal/nodes/connected")
                response.raise_for_status()
                nodes = _ConnectedNodes.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as exc:
            raise NodeCommandError(
                "node_routing_unavailable", "Cannot read connected nodes", "none"
            ) from exc
        return set(nodes.machine_ids)


@lru_cache(maxsize=1)
def node_command_client() -> NodeCommandClient:
    """Reuse environment-derived routing configuration within this runner."""
    return NodeCommandClient.from_environment()
