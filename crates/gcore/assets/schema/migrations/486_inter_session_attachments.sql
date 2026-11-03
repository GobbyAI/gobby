-- A document attached to one inter-session message. The message id is the key, so an
-- attachment cannot outlive its message: retention and session cascades delete both.
-- Readers are the message's sender and recipient; the daemon enforces that per query.
CREATE TABLE inter_session_attachments (
    message_id uuid PRIMARY KEY REFERENCES inter_session_messages (id) ON DELETE CASCADE,
    filename text NOT NULL CHECK (
        btrim(filename) <> '' AND length(filename) <= 255 AND filename !~ '[/\\[:cntrl:]]'
    ),
    media_type text NOT NULL CHECK (media_type IN ('text/plain', 'text/markdown')),
    content text NOT NULL CHECK (octet_length(content) <= 262144),
    size_bytes integer NOT NULL CHECK (size_bytes = octet_length(content)),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT, INSERT, DELETE ON inter_session_attachments TO gobby_daemon_runtime;
