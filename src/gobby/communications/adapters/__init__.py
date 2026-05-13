from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.communications.adapters.base import BaseChannelAdapter

_ADAPTER_REGISTRY: dict[str, type[BaseChannelAdapter]] = {}
logger = logging.getLogger(__name__)


def register_adapter(channel_type: str, adapter_class: type[BaseChannelAdapter]) -> None:
    """Register an adapter class for a channel type."""
    _ADAPTER_REGISTRY[channel_type] = adapter_class


def get_adapter_class(channel_type: str) -> type[BaseChannelAdapter] | None:
    """Get the adapter class for a channel type."""
    return _ADAPTER_REGISTRY.get(channel_type)


def list_adapter_types() -> list[str]:
    """List all registered adapter types."""
    return sorted(_ADAPTER_REGISTRY.keys())


# Import adapters to register them.
for _adapter_module in (
    "gobby.communications.adapters.slack",
    "gobby.communications.adapters.telegram",
    "gobby.communications.adapters.discord",
    "gobby.communications.adapters.teams",
    "gobby.communications.adapters.email",
    "gobby.communications.adapters.sms",
    "gobby.communications.adapters.gobby_chat",
):
    try:
        importlib.import_module(_adapter_module)
    except ImportError as exc:
        logger.debug("Skipping communications adapter %s: %s", _adapter_module, exc)

del _adapter_module
