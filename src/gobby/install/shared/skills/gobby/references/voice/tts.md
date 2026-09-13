# Speech playback

Load before choosing a TTS provider, preparing reference audio, or recovering
playback. Fetch configuration schemas and load `configuration.md` before writes.
Inspect `/api/voice/status` for provider, backend, capabilities, availability,
and warmup errors. These are HTTP/browser operations; `gobby-voice` only exposes
vocabulary tools.

1. Select the intended provider and configure its reference. Chatterbox is
   embedded and requires a clip longer than five seconds; a clean 10–20 second
   single-speaker WAV is the guide's preparation recommendation. It ignores
   reference text. Crane requires a readable nonempty WAV and exact nonempty
   transcript, and sends both with each utterance to its configured service.
2. For Crane, the operator manages installation, startup, readiness, and shutdown
   outside Gobby. Verify that service's `/ready` endpoint and request size limit;
   Gobby does not supply or manage its process.
3. Refresh an existing cached provider after configuration changes. Enable the
   browser speaker and let `voice_prepare` warm TTS. Availability alone does not
   establish readiness or successful synthesis.
4. Verify a short reply, interrupt a longer one, then play another short reply.
   `tts_stop` cancels the conversation pipeline and browser playback clears its
   queue. Do not send a new prompt just to test playback without authorization.

Text is normalized and split into bounded clauses before synthesis.
Chatterbox's default clause bound is 180 characters and generation cap 1000
tokens; schema limits remain authoritative. Crane requires mono PCM16 streams
with valid sample-rate metadata, buffers an initial half-second where possible,
and rejects empty audio or a trailing partial sample. A failed HTTP stream is
not automatically replayed. Inspect provider errors and fix reference/service
health before retrying. Closing Gobby's HTTP stream does not prove the external
service has finished its own inference operation.

See [TTS providers](../../../../../../../../docs/guides/voice.md#tts-provider)
and [Crane setup and restoration](../../../../../../../../docs/guides/crane-tts.md).
