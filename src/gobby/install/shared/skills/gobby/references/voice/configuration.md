# Voice configuration

Load before changing voice switches, models, providers, or reference paths.
Fetch `gobby-config:get_config_values` and `patch_config_values` schemas;
load `gobby:references/config/updates.md` for the configuration write contract.

1. Read desired and active values and the current revision. Save only the
   settings being changed when a trial needs restoration.
2. Patch nested values such as `{"voice":{"enabled":true}}` with the returned
   `expected_revision`. Re-read
   after a conflict; do not overwrite unrelated concurrent changes.
3. Check the response's active/pending state. The master `voice.enabled`
   defaults false; browser `stt_enabled` and `tts_enabled` default true but
   require the master switch. Browser preferences are separate request intent.
4. Refresh loaded voice models after a provider/model/reference change.
   Merely opening another browser conversation does not replace the cached
   provider. Models are released by voice cleanup when no web chats remain.
   Coordinate a daemon restart if needed, especially for new adapter code.
5. Query voice status for the intended STT/TTS targets and warm through the
   browser controls before treating the configuration as operational.

Default TTS is `chatterbox`; `crane` uses a manually managed service. Both use
`tts_reference_audio`; Crane also requires a nonempty `tts_reference_text`.
Use the TTS topic for provider-specific validation and recovery.

`voice.openai_compatible_audio` configures HTTP transcription/translation
bindings independently of the browser Whisper switches. Each binding names a
provider, API URL including `/v1`, model, and capability toggles. Persist API
keys through `$secret:NAME` references. Explicitly select the intended provider
when local processing matters; a configured endpoint can receive recordings.

Operator-only `gobby install voice` enables voice on an existing installation;
`uv sync` repairs the daemon checkout's dependency environment. Neither is an
MCP vocabulary operation. Restore trials with a fresh revision and only their
changed values, then refresh models and verify status again.

See [configuration](../../../../../../../../docs/guides/voice.md#configuration).
