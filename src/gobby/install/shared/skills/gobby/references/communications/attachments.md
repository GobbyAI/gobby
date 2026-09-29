# Attachments

Load before sending a local file. Fetch `gobby-communications:send_attachment`
and discover the intended channel before preparing an authorized delivery.

1. Select an existing regular file inside the resolved workspace. The tool
   expands and resolves paths, including symlinks, before checking containment.
2. Supply the channel and file path; optional caption, session, filename,
   content type, and metadata follow the current schema. The default MIME
   type is inferred from the supplied filename or path, then falls back to
   `application/octet-stream`.
3. Load `routing.md` for destination metadata and session threading.
4. Inspect the returned message status/error and attachment metadata.
   The manager checks adapter-specific size limits before delivery.

A missing workspace, nonexistent path, directory, or outside-workspace file
fails before sending. Select the intended workspace file; do not bypass the
containment check. An inactive channel, unsupported adapter attachment method,
or provider failure requires correcting that cause before retrying.

To send text as a document without a file, operators use
`gobby comms attach CHANNEL_NAME FILENAME`, which reads stdin, or the
authenticated `POST /api/comms/attachment` endpoint. Both are content-fed and
read no host path. The CLI scrubs secrets and home paths first. When the
redacted content exceeds 64 KiB, the CLI sends a one-line omission note instead
of the document. The route takes a bare `.txt` or `.log` filename and at most 64 KiB, and always
sends the content as `text/plain`.

Telegram uses photo, voice, or document delivery according to the media.
Long non-voice captions continue as separate text messages. A continuation
failure can follow a successful media upload; inspect delivery evidence before
retrying the whole attachment to avoid duplicates.
See [attachment sends](../../../../../../../../docs/guides/telegram.md#mcp-sends)
and [provider limits](../../../../../../../../docs/guides/telegram.md#limits-and-dependencies).
Documenting an attachment workflow does not authorize uploading user files.
