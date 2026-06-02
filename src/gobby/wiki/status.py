from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from gobby.gwiki_gateway import GwikiCommandError, GwikiGatewayError


class WikiStatusGateway(Protocol):
    async def status(self) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...


async def collect_wiki_status(
    *,
    gateway: WikiStatusGateway,
    runner: object | None,
) -> dict[str, Any]:
    status_envelope = await _gateway_probe(lambda: gateway.status())
    health_envelope = await _gateway_probe(lambda: gateway.health())
    payload = dict(_payload(status_envelope))
    gateway_maintenance = _gateway_maintenance(health_envelope)
    watcher_maintenance = _watcher_maintenance(runner)
    payload["maintenance"] = {
        "watcher": watcher_maintenance,
        "gateway": gateway_maintenance,
        "degraded": bool(gateway_maintenance["degraded"]),
    }

    result = dict(status_envelope)
    result["payload"] = payload
    return result


async def _gateway_probe(
    call: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    try:
        return await call()
    except GwikiCommandError as exc:
        return exc.to_envelope()
    except GwikiGatewayError as exc:
        return {
            "ok": False,
            "status": "failed",
            "payload": None,
            "stderr": "",
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
        }


def _gateway_maintenance(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(envelope)
    error = envelope.get("error")
    status = _text(payload.get("status")) or _text(envelope.get("status")) or "unknown"
    degraded_services = _string_list(payload.get("degraded_services"))
    degraded = (
        bool(error)
        or bool(degraded_services)
        or status.lower() not in {"healthy", "ready", "ok", "completed"}
    )
    return {
        "available": bool(envelope.get("ok", False)) and not bool(error),
        "status": status,
        "degraded": degraded,
        "degraded_services": degraded_services,
        "error": error if isinstance(error, dict) else None,
    }


def _watcher_maintenance(runner: object | None) -> dict[str, Any]:
    watcher = getattr(runner, "_wiki_watcher", None) if runner is not None else None
    health = _watcher_health(watcher)
    if health is None:
        return {
            "active": False,
            "running": False,
            "scope_count": 0,
            "last_index_time": None,
            "pending_debounce": False,
            "pending_changes": 0,
        }
    return {
        "active": True,
        "running": bool(health.get("running", False)),
        "scope_count": _int_value(health.get("scope_count")),
        "last_index_time": health.get("last_index_time"),
        "pending_debounce": bool(health.get("pending_debounce", False)),
        "pending_changes": _int_value(health.get("pending_changes")),
    }


def _watcher_health(watcher: object | None) -> dict[str, Any] | None:
    health_call = getattr(watcher, "health", None)
    if not callable(health_call):
        return None
    value = health_call()
    return value if isinstance(value, dict) else None


def _payload(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = envelope.get("payload")
    return payload if isinstance(payload, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0
