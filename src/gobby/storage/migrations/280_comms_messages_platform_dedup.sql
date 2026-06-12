DELETE FROM comms_messages newer
USING comms_messages older
WHERE newer.channel_id = older.channel_id
  AND newer.platform_message_id = older.platform_message_id
  AND newer.platform_message_id IS NOT NULL
  AND newer.ctid > older.ctid;

CREATE UNIQUE INDEX IF NOT EXISTS idx_comms_messages_channel_platform_message
ON comms_messages(channel_id, platform_message_id)
WHERE platform_message_id IS NOT NULL;
