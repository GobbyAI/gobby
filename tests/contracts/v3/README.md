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
(names only). The routes answer 503 `Communications manager not available` when no manager is
running, so the block is what lets the list, send and channel cases answer at all. The voice family's startup entry
(`startup.voice`) holds only `config`, with `voice.enabled` and `voice.stt_enabled` set to `true`.
The other families have no startup entry and start from the default daemon.

## Seeded channel

The startup channel is `corpus-slack`, a Slack channel with the fixed id
`5b0c7a1e-3f2d-4c8a-9e61-0c0a0c0a0001` and the destination `C0CORPUS0001`. The channel's
`default_destination` is required: a Slack send with no destination in its metadata or attached
session fails without it (`src/gobby/communications/outbound.py:205-220`,
`src/gobby/communications/adapters/base.py:180-191`). Its bot token and signing secret are
`$secret:` references. The recorder generates each secret value at run time and writes it under
the daemon's home, so the recorded config shows the `$secret:` reference, not the value. The
gobby_chat row that a communications-enabled daemon creates at every start is recorded too, with
its random id normalized and its timestamps masked.

## Provider captures and message rows

Provider hits go through the loopback edge in `tests/contracts/comms/`, which logs each request's
host, method, path and sanitized `semantic` fields (`destination`, `text`, `filename`,
`payload_sha256`). A case's `provider_edge` side effect is that ordered list for the capture
window, compared entry by entry, so a replay that reaches a different endpoint, skips a step, or
sends a different destination, text or attachment with the same reply is a difference. The Slack
channel is checked for `active` and quiet before a capture window opens, so the channel's own
initialization is never recorded as a case side effect.

The three persisted sends (`comms_send`, `comms_attachment`, `comms_mcp_send_message`) also
declare a `comms_messages` table delta and record the one outbound row each writes.
`comms_send_missing_channel`, the refused send, declares the same Slack capture and table delta
and records both empty: a refusal reaches no provider and writes no row.

## Unordered collections

The channel list answers (`comms_channels_list`, `comms_mcp_list_channels`) come from a read with
no `ORDER BY` (`list_channels`, `src/gobby/storage/communications.py:84-92`), so row order is not part of the contract. Each declares the collection rule
`{"name": "unordered", "key": "name"}` on its list pointer. The Python and Rust normalizers sort
that list by the JSON text of the key field, on the recording and the replay alike, before the
case's positional normalizers and masks apply. A reversed list matches; a changed channel is a
difference. The rule is legal only among a case's own normalizers, not in a table delta or a side
effect.

## Route coverage

A v3 http case earns ledger coverage only for the route its request reaches. The Python loader
(`tests/contracts/corpus_v3.py`) matches the request path to the ledger key's route template one
segment at a time: a literal segment must be equal and a `{parameter}` segment matches exactly one
non-empty segment, so a response relabelled onto another route of the same method is refused. The
Rust loader does not apply this check.

## What the proxy self-replay proves

`v3_cases_proxy_self_replay` (`crates/gdaemon/tests/http_replay/mod.rs`) replays every case
through the Rust front door to a stub target that answers with the recorded response, and its
observed table deltas and side effects are the recorded ones. It therefore proves that the Rust
loader accepts every case, that the normalize and compare plumbing returns no difference for an
unchanged recording, and that the comms and voice paths route to the proxy. It does not prove
native parity: no Rust implementation of these routes runs, and nothing is observed from one.
Native parity needs a replay against a native route whose response, table deltas and provider-edge
capture are observed from the Rust code, not served from the recording.

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

No case records the voice routes with voice disabled. With voice off the grant carries no audio
capability and the routes refuse with a 401 `wrong_capability` envelope; that refusal was recorded once and dropped when the voice cases moved to a voice-enabled daemon,
so it is not part of this corpus.

## Uncovered voice WebSocket rows

The voice WebSocket rows `tts_stop`, `voice_audio`, `voice_mode_toggle`, `voice_prepare` and
`voice_status_request` are not in any HTTP or MCP case. No case in this directory covers them. The
VOICE chunk's oracle in `docs/port/map-v2.md` records their frame sequences in its own first
checkpoint, under `tests/contracts/voice/`.
