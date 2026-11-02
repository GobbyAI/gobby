-- Telegram message ids are unique only within one chat, so the platform key must include the destination.
DROP INDEX public.idx_comms_messages_channel_platform_message;
CREATE UNIQUE INDEX idx_comms_messages_channel_platform_message ON public.comms_messages (
    channel_id,
    (COALESCE(metadata_json->>'platform_destination', '')),
    platform_message_id
) WHERE platform_message_id IS NOT NULL;
