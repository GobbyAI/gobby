"""Daemon error response formatting shared by CLI commands."""

from collections.abc import Mapping
from typing import Any


def _daemon_error_detail(response: Any) -> Any:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        return payload.get("detail", payload)
    return payload


def _daemon_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or detail.get("error")
        if isinstance(message, Mapping):
            nested = message.get("message") or message.get("detail") or message.get("error")
            if nested is not None:
                return str(nested)
        return str(message) if message is not None else str(detail)
    return str(detail)
