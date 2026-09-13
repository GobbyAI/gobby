# Voice diagnostics

Load for hidden controls, failed recognition, warmup, reference errors, or silent
playback. Begin read-only: `GET /api/voice/status`, relevant logs, and
`gobby-config:get_config_values` after schema discovery. `gobby-voice:list_vocab`
inspects active terminology without recording audio.

Check configuration, availability, warmup, then playback in that order.

- Disabled browser voice: inspect master and side switches plus browser intent.
  HTTP `want_stt`/`want_tts` scope readiness; omitted targets use config-enabled
  sides. Ready requires at least one requested side and all requested sides ready.
- Missing packages: the operator runs `uv sync` in the daemon checkout. A model
  load can still fail after package availability passes; read warmup errors.
- Microphone unavailable: verify HTTPS/localhost and microphone permission.
  For short/empty audio, record a complete phrase; do not equate empty text with
  a provider failure.
- Wrong terms: inspect vocabulary and configured prompt, then compare desired
  and active values. Refresh a cached Whisper instance after relevant changes.
- TTS unavailable: inspect provider ID, reference path, provider capabilities,
  and `tts_reason`. Chatterbox needs more than five seconds; Crane needs a valid
  nonempty WAV, transcript, reachable service, and correctly framed PCM.
- Warmup stuck: scope status to the side actually requested, read its error,
  correct the cause, and retry through the browser. A status read does not warm
  models, and available packages do not mean warmed models.
- Silent or interrupted playback: distinguish browser audio unlock/queue state
  from synthesis failure. Inspect `tts_status`; cancel the current pipeline
  before an authorized fresh attempt. Do not automatically replay failed speech.

Operator recovery can include releasing voice sessions, switching compute
device through revisioned config, or a coordinated restart. Never restart the
shared daemon or start/stop Crane merely because this reference was loaded.
Use isolated fixtures for mutating diagnostics and preserve private recordings.

See [troubleshooting](../../../../../../../../docs/guides/voice.md#troubleshooting)
and [Crane recovery](../../../../../../../../docs/guides/crane-tts.md#restore-the-previous-local-settings).
