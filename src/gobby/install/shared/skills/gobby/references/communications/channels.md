# Channels

Load when discovering, adding, configuring, diagnosing, or removing a channel.
Fetch the relevant `gobby-communications` tool schema before use.

1. Call `list_channels` and retain the exact channel name and UUID.
2. Inspect the returned channel's `status.active` and `status.init_error`;
   an enabled stored row may have failed initialization. Active channel status
   includes `is_polling`, `supports_webhooks`, and `supports_polling`;
   inactive status omits those fields.
3. For authorized setup, use `add_channel` with adapter-specific non-secret
   `config` and separate `secrets`. Credentials become `$secret:` references.
4. Recheck status after setup; repair credentials or configuration before
   attempting delivery. Unknown adapter types can be stored but cannot activate.
5. Use `remove_channel` with its name only when removal is requested.

Operator entrypoints are `gobby comms status`, `gobby comms channels list`,
`add`, and `remove`. HTTP channel CRUD uses `/api/comms/channels` and UUID
paths; `/api/comms/channels/{channel_id}/status` reports initialization state.
When `config` is supplied, HTTP `PUT` replaces non-secret configuration wholesale. Preserve all
required fields through read-modify-write; omitted existing secret references
are retained. Updates deactivate the adapter and reinitialize it if enabled.

Top-level communications enablement and webhook origin require operator
configuration and daemon lifecycle work. Do not infer active settings from
bundled defaults. See [channel management](../../../../../../../../docs/guides/comm-integrations.md#manage-channels)
and [safe Telegram updates](../../../../../../../../docs/guides/telegram.md#safe-channel-updates).
