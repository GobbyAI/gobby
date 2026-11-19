# Provider edge for the communications corpus

The communications and voice recordings never reach a provider. Each run starts one loopback
provider edge, `fake_edge.py`, and points the daemon's HTTPS at it. The edge is a CONNECT proxy on
`127.0.0.1` that terminates TLS with a per-run test certificate authority and never forwards
upstream. The authority lives in the run's scratch directory and is trusted through
`SSL_CERT_FILE`.

Scripts in `edge/` give the replies for each provider host:

- `slack.json`: `auth.test`, `chat.postMessage`, `files.getUploadURLExternal`, the upload PUT on
  `upload.corpus.test`, and `files.completeUploadExternal`, each with `ok: true`.
- `telegram.json`, `teams.json`, `sms.json`: the default reply for each host. Teams also serves a
  public RSA key set at `/v1/.well-known/keys` on `login.botframework.com`.
- `email.json`: the mailbox login that the IMAP and SMTP listeners accept.

A host no script names answers HTTP 200 with `{"ok": true}`, and that host is still logged
The test authority mints a leaf certificate for each CONNECT host on first use. The IMAP listener
speaks implicit TLS and the
SMTP listener speaks STARTTLS, both with a certificate for `127.0.0.1`.

Every request through the proxy is logged as host, method and path, in arrival order. A Telegram
bot token in a path is replaced with `/bot@TELEGRAM_TOKEN@` before it is logged. The CONNECT itself is
not logged.

`credential_scan.py` checks every committed v3 case and every edge script for Slack and Telegram
token shapes listed in `SHAPES`. It reports the file and the shape, never a
matched value.

The adapter section is added in a later commit of this chunk.
