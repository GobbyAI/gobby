# Voice

Load when configuring speech recognition, speech playback, Whisper vocabulary,
or diagnosing voice availability. `$gobby voice references` lists topics;
menus do not record audio, change configuration, or start services.

Discover tools on `gobby-voice`; fetch each required schema before calling it.
The four vocabulary tools are agent-supported. General voice settings use
`gobby-config`; installation and external service lifecycle are operator
procedures. A voice request does not authorize sending a transcript to another
session or an external audio provider.

1. Load `vocabulary.md` to inspect, add, remove, or clear Whisper terms.
2. Load `configuration.md` before changing voice settings or providers.
3. Load `stt.md` for microphone input and one-shot transcription/translation.
4. Load `tts.md` for Chatterbox, Crane, reference recordings, and interruption.
5. Load `diagnostics.md` for package, model, readiness, or playback failures.

Start with status and the intended surface: browser voice capture uses local
Whisper, while the HTTP audio endpoint can select configured compatible
providers. TTS can use an embedded model or a separately managed service.
Check errors and desired versus active configuration before retrying.

See the [voice guide](../../../../../../../../docs/guides/voice.md) and
[Crane setup](../../../../../../../../docs/guides/crane-tts.md).
