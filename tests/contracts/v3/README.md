# Schema 3 HTTP, MCP, WebSocket and CLI corpus

This directory holds the schema 3 cases. The manifest lists every case file, in replay order.
The loader refuses any other JSON file here, except the declared vector `grant_encoding.json`.
Raw templates live in `templates/`, and the loader never reads them. The recorder
(`python -m tests.contracts.recorder_v3`) reads the templates and writes the recorded case files to
its `--out` directory. The manifest and the templates are committed by hand.

## Communications enablement

The communications cases run in a daemon whose startup block enables communications. The manifest
key `startup.communications` holds three entries, applied before that daemon starts: `config`
(`communications.enabled` set to `true`), `channels` (one Slack row with a fixed id) and `secrets`
(names only). The routes answer 503 `Communications manager not available` when no manager is running, so the
block is what lets the list, send and channel cases answer at all. The voice and other families have no
startup entry and start from the default daemon.

## Seeded channel

The startup channel is `corpus-slack`, a Slack channel with the fixed id
`5b0c7a1e-3f2d-4c8a-9e61-0c0a0c0a0001` and the destination `C0CORPUS0001`. The channel's
`default_destination` is required: a Slack send with no destination in its metadata or attached
session fails without it (record line 20). Its bot token and
signing secret are `$secret:` references. The recorder generates each secret value at run time,
writes it under the daemon's home. The recorded config shows the `$secret:` reference, not the
value. The gobby_chat row that a
communications-enabled daemon creates at every start is recorded too, with its random id
normalized and its timestamps masked.

Provider hits go through the loopback edge in `tests/contracts/comms/`, which logs each request's
host, method and path. The Slack channel is checked for `active` and quiet before a capture window
opens, so the channel's own initialization is never recorded as a case side effect.

## Voice masks

Voice is disabled by default and the corpus seeds no voice key. The voice routes are grant-gated.
The three voice cases present a runtime grant (`X-Gobby-Runtime-Grant`) that the daemon's own
handshake issues for the recording machine and project. Each case then refuses the grant with HTTP
401 and the code `wrong_capability`, because the grant carries the `audio_transcribe` capability
only when speech-to-text is enabled. The recorded refusal is the answer, so these cases carry no
masks: the engine, model and availability fields of a 200 answer do not appear in them.

## Uncovered voice WebSocket rows

The voice WebSocket rows `tts_stop`, `voice_audio`, `voice_mode_toggle`, `voice_prepare` and
`voice_status_request` are not in any HTTP or MCP case. They are listed as uncovered
(plan section 2.2, which cites `docs/port/map-v2.md:1558`; this pass did not confirm that
citation). No case in this directory covers them.
