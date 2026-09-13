# Crane TTS setup and recovery

Crane is an optional external TTS provider. The repository default remains
`chatterbox`. Gobby sends each normalized sentence and its reference audio to the
manually started service, then uses the existing WebSocket PCM playback path.
Gobby never starts or stops the Crane process.

## Local setup

Crane installation and process management are operator procedures outside Gobby.
Start the service using its own installed launcher or service manager. Gobby
does not install a launcher at a fixed path. For the default configured URL,
check readiness with:

```sh
curl --fail http://127.0.0.1:8080/ready
```

Use the Gobby runtime configuration API (`gobby-config:get_config_values`, then
`gobby-config:patch_config_values` with the returned `expected_revision`) to set:

- `voice.tts_provider`: `crane`
- `voice.tts_crane_url`: `http://127.0.0.1:8080` (the default)
- `voice.tts_reference_audio`: a private, readable, nonempty WAV file
- `voice.tts_reference_text`: the accurate, nonempty transcript of that recording

Keep `voice.enabled` and `voice.tts_enabled` enabled. Enable the speaker control
in web chat. Release cached voice models after changing providers: opening
another conversation alone reuses the singleton. Voice cleanup unloads models
when no web chat sessions remain. Inspect desired/active config and status,
then warm the selected provider again. New adapter code requires a coordinated
daemon restart. See the [voice guide](./voice.md#configuration).

Keep private reference files and configuration snapshots outside the repository.
Check the installed Crane service's request-size limit. Gobby embeds the WAV
as base64 in every JSON speech request, expanding it by roughly one third.
Prepare a compact reference with room for its transcript and the utterance;
the adapter does not enforce a universal Crane request limit. For example,
convert to mono PCM16 at 24 kHz without trimming the recording, so its transcript
remains accurate:

```sh
ffmpeg -i /private/path/reference.wav -ac 1 -ar 24000 -c:a pcm_s16le \
  /private/path/crane-reference.wav
```

The adapter requests streamed PCM and requires `audio/pcm`, `x-audio-channels: 1`,
`x-audio-format: s16le`, and a sample rate from 8,000 through 192,000 Hz. It retains
partial samples across HTTP reads. Empty or malformed audio, readiness failures,
HTTP errors, and interrupted bodies use the existing voice error status. A failed
response is never automatically replayed. Interruption closes the HTTP stream;
the external service owns when its inference work stops. Gobby buffers at least
half a second before its first emission when the stream is long enough, and
rejects a final partial PCM sample.

## Restore the previous local settings

Before activation, save the desired voice settings and web speaker preference in
a private file outside the repository; no pre-existing trial snapshot is assumed.
To restore, fetch the current configuration revision, then patch only the fields
changed by the trial using the saved values:

- `voice.tts_provider`
- `voice.tts_reference_audio`
- `voice.tts_reference_text`
- `voice.tts_crane_url`, if changed
- `ui_settings.ttsEnabled`, if changed

Leave unrelated settings at their current values. Release the current voice
session or restart the daemon in a coordinated quiet window. Stop Crane manually
in its terminal when it is no longer needed. Keep the original reference intact.

For acceptance, request a short assistant reply, interrupt a longer reply while
audio is playing, and request another short reply. Confirm PCM reaches browser
playback, interruption clears queued audio, and the next reply plays. Latency is
informational and has no acceptance threshold. Attached CLI speech now shares
the voice integration; select the intended session before recording.

_Last verified: 2026-09-12_
