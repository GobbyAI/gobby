# Speech recognition

Load for microphone capture, transcription, translation, or recognition errors.
Start with `GET /api/voice/status`; this is an HTTP/browser surface, not an
additional `gobby-voice` MCP tool. Use the vocabulary topic for MCP terminology
edits and configuration topic for provider setup.

Browser capture uses local faster-whisper. Enable voice/STT in daemon config,
grant microphone access in HTTPS or localhost, then use the chat microphone's
off → Push to Talk → VAD → off cycle. In Push to Talk, the empty composer's
primary record button supports hold/release or tap-to-start/tap-to-submit;
Escape cancels. VAD detects speech boundaries. `voice_prepare` warms only the requested,
config-enabled targets. `voice_audio` carries base64 audio, MIME type,
conversation and request identifiers; successful transcription is also submitted
as a chat message. An attached-session target routes that text to its CLI session.
Recording and submission require user intent; a diagnostic menu does neither.

The operator/client HTTP endpoint `POST /api/voice/transcribe` accepts multipart
`file` and optional `capability` (`audio_transcribe` default or `audio_translate`),
`provider`, `model`, `language`, and `prompt`. It returns text, segments, detected
language, task, byte/MIME details, and selected capability/provider/model.
It does not submit the result to chat. Pin `provider=whisper` for the local
binding; compatible bindings may send the recording to their configured URL.
Compatible adapters forward prompt overrides and transcription language;
translation omits language. The local
Whisper adapter uses its configured initial prompt and automatic language detection.

Uploads use the server attachment size limit. Whisper rejects tiny recordings
before loading a model; empty recognized speech is distinct from an error.
On timeout or provider failure, inspect the returned error before retrying.
HTTP 400 indicates invalid/unavailable selection, 503 an unavailable provider,
504 timeout; a 413 upload needs a smaller file. Check dependency health and
actual configuration instead of switching providers silently.

See [audio API](../../../../../../../../docs/guides/voice.md#api-and-websocket-reference)
and [troubleshooting](../../../../../../../../docs/guides/voice.md#troubleshooting).
