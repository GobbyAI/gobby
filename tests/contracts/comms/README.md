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

A host no script names answers HTTP 200 with `{"ok": true}`, and that host is still logged.
The test authority mints a leaf certificate for each CONNECT host on first use. The IMAP listener
speaks implicit TLS and the SMTP listener speaks STARTTLS, both with a certificate for
`127.0.0.1`.

Every request through the proxy is logged as host, method and path, in arrival order. A Telegram
bot token in a path is replaced with `/bot@TELEGRAM_TOKEN@` before it is logged. The CONNECT itself is
not logged.

`credential_scan.py` checks every committed v3 case and every edge script for Slack and Telegram
token shapes listed in `SHAPES`. It reports the file and the shape, never a
matched value.

## Adapter wire recordings

`adapter_wire/` holds one fixture per adapter and operation: `send`, `inbound` and `attachment`
for Slack, Telegram, Teams, email and SMS, 15 files in all. `adapter_branch.json` states the
oracle branch of each adapter. No adapter has a real account, so these five take the
published-API-contract branch, unverified, and Discord is deferred. No fixture is an oracle:
every one says `oracle: false` and `verified: false`, and its `contract_ref` names the provider
reference that its scripted responses follow. `source_file` and `source_test` name the adapter
module and the existing adapter test whose shapes the drive follows.

Each fixture runs the Python adapter in-process against an edge built from `fake_edge.py`. The
edge answers only from the fixture's own `provider_contract.responses`, in order, and a request
that does not match the next response answers HTTP 404 (or an SMTP or IMAP refusal). A connect
to, or a name lookup of, any non-loopback address is recorded and refused, so a replay never
reaches a provider. Each provider request is recorded as its host, method and path (a Telegram
bot token in a path is masked) and four sanitized fields: `destination`, `text`, `filename` and
`payload_sha256` (the SHA-256 of the uploaded bytes, or of a Teams card's content). `outcome` is
what the adapter returned: the platform message id for a send, the parsed messages (with no
clock or generated ids) for an inbound, or the error for a refusal.

- `capture` is `exchange` when the adapter ran the operation and `refusal` when it refused
  before any request. `sms_attachment` is the one refusal: an MMS needs a public
  `platform_url`, and the recording has none. Slack and SMS inbound are webhooks that the
  adapter verifies and parses in-process, so they are exchanges with no provider request.
- Email speaks SMTP and IMAP to the edge's loopback TLS listeners with password login. The
  adapter keeps its configured ports (587 selects STARTTLS, 993 is implicit TLS); only the
  connect port is moved to the edge. An SMTP submission is recorded as host `127.0.0.1`,
  method `SMTP`, path `DATA`; the IMAP mailbox commands as method `IMAP` with the command as
  path. The generated Message-ID is pinned to the drive's value.
- `email_inbound` records a Python defect: aioimaplib returns the fetched message as a
  `bytearray`, and the adapter accepts only `bytes`, so every polled message is dropped and
  never marked seen. The fixture shows three mailbox commands and no messages. It goes red, and
  is re-recorded, when the adapter is fixed.
- The Slack webhook is verified with the adapter's clock frozen at the drive's instant, so the
  signature check passes on every replay.
- The Teams webhook token is signed with a key pair minted for each replay. The fixture holds
  the token's claims and a key-set body with marker values, which the replay replaces with the
  minted public key, so no private key is committed.

Record with `GOBBY_RECORD_ADAPTER_WIRE=1` and
`tests/contracts/comms/test_adapter_wire.py::test_record_adapter_wire`. The recorder refuses an
error outcome for any fixture but the refusal, and a request sequence that differs from the
scripted responses. A replay through this edge proves the request shape and the adapter's
handling of the published responses, not parity with a real provider.
