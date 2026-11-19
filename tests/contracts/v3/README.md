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
block is what lets the list, send and channel cases answer at all. The voice family's startup entry
(`startup.voice`) holds only `config`, with `voice.enabled` and `voice.stt_enabled` set to `true`.
The other families have no startup entry and start from the default daemon.

## Seeded channel

The startup channel is `corpus-slack`, a Slack channel with the fixed id
`5b0c7a1e-3f2d-4c8a-9e61-0c0a0c0a0001` and the destination `C0CORPUS0001`. The channel's
`default_destination` is required: a Slack send with no destination in its metadata or attached
session fails without it (`src/gobby/communications/outbound.py:205-220`,
`src/gobby/communications/adapters/base.py:180-191`). Its bot token and
signing secret are `$secret:` references. The recorder generates each secret value at run time,
writes it under the daemon's home. The recorded config shows the `$secret:` reference, not the
value. The gobby_chat row that a
communications-enabled daemon creates at every start is recorded too, with its random id
normalized and its timestamps masked.

Provider hits go through the loopback edge in `tests/contracts/comms/`, which logs each request's
host, method and path. The Slack channel is checked for `active` and quiet before a capture window
opens, so the channel's own initialization is never recorded as a case side effect.

## Voice masks

The voice cases run with voice and speech-to-text enabled by the voice startup entry. The voice
routes are grant-gated, and the grant carries the `audio_transcribe` capability only when both keys
are on (`src/gobby/runtime_grants/service.py:177-181`). The three HTTP voice cases present a runtime
grant (`X-Gobby-Runtime-Grant`) that the daemon's own handshake issues for the recording machine and
project. No speech engine is installed or downloaded for the corpus: nothing warms a model, so the
warmup statuses are `idle`.

`voice_status` and `voice_status_want_tts` answer 200. Every key of the status body is masked under
`/response/body/` except the three flags `enabled`, `stt_enabled` and `tts_enabled`, which a replay
must match. The masked keys are the engine-naming values (`whisper_model`, `tts_provider`), the
availability and warmup fields, the TTS provider's detail keys (its reference-audio path, which
holds the daemon's home, its device and its token cap) and the audio capability flags. A mask hides
a value, not a key, so a replay must still answer the same key set; the TTS provider's detail key
names (`tts_chatterbox_max_generation_tokens` among them) are part of the recorded contract.
`voice_transcribe_missing_file` answers 422 with the request-validation envelope for the missing
`file` field, before any engine runs.

## Uncovered voice WebSocket rows

The voice WebSocket rows `tts_stop`, `voice_audio`, `voice_mode_toggle`, `voice_prepare` and
`voice_status_request` are not in any HTTP or MCP case. No case in this directory covers them. The
VOICE chunk's oracle in `docs/port/map-v2.md` records their frame sequences in its own first
checkpoint, under `tests/contracts/voice/`.
