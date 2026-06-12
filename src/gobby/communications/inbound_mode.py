"""Inbound-mode decisions shared by communications runtime setup."""

from dataclasses import replace

from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.models import ChannelConfig


def should_poll(adapter: BaseChannelAdapter, webhook_base_url: str) -> bool:
    """Return whether the adapter should poll for inbound messages."""
    return adapter.supports_polling and (not adapter.supports_webhooks or not webhook_base_url)


def channel_for_adapter_init(
    channel: ChannelConfig, adapter: BaseChannelAdapter, webhook_base_url: str
) -> ChannelConfig:
    """Return channel config with manager-selected inbound mode for adapter init."""
    if channel.channel_type != "telegram":
        return channel
    config_json = dict(channel.config_json)
    if adapter.supports_webhooks and webhook_base_url:
        config_json["webhook_base_url"] = webhook_base_url
    else:
        config_json.pop("webhook_base_url", None)
    if config_json == channel.config_json:
        return channel
    return replace(channel, config_json=config_json)
