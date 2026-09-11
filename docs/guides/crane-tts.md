# Crane TTS web trial

Crane is an optional external TTS provider. The repository default remains
`chatterbox`. Gobby sends each normalized sentence and its reference audio to the
manually started service, then uses the existing WebSocket PCM playback path.
Gobby never starts or stops the Crane process.

## Local setup

Start the installed launcher in a terminal:

```sh
~/.local/bin/crane-tts
curl --fail http://127.0.0.1:8080/ready
```

Use the Gobby runtime configuration API (`gobby-config:get_config_values`, then
`gobby-config:patch_config_values` with the returned `expected_revision`) to set:

- `voice.tts_provider`: `crane`
- `voice.tts_crane_url`: `http://127.0.0.1:8080` (the default)
- `voice.tts_reference_audio`: a private, readable, nonempty WAV file
- `voice.tts_reference_text`: the accurate, nonempty transcript of that recording

Keep `voice.enabled` and `voice.tts_enabled` enabled. Enable the speaker control
in web chat. Start a fresh voice session after changing providers; a running
provider is released when voice sessions are unloaded. New adapter code requires
a coordinated daemon restart.

Keep private reference files and configuration snapshots outside the repository.
Crane's speech endpoint uses a 2 MiB JSON request limit. Base64 expands audio by
roughly one third, so prepare a compact reference with room for the transcript
and sentence. A 30-second mono PCM16 WAV at 24 kHz fits. Convert without trimming
the recording, so its transcript remains accurate:

```sh
ffmpeg -i /private/path/reference.wav -ac 1 -ar 24000 -c:a pcm_s16le \
  /private/path/crane-reference.wav
```

The adapter requests streamed PCM and requires `audio/pcm`, `x-audio-channels: 1`,
`x-audio-format: s16le`, and a sample rate from 8,000 through 192,000 Hz. It retains
partial samples across HTTP reads. Empty or malformed audio, readiness failures,
HTTP errors, and interrupted bodies use the existing voice error status. A failed
response is never automatically replayed. Interruption closes the HTTP stream;
Crane releases generation after its current inference operation completes.

## Restore the previous local settings

Before activation, save the desired voice settings and web speaker preference in
a private file. This trial uses `~/.gobby/voice/crane-trial-prior-settings.json`.
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
informational and has no acceptance threshold. Terminal speech is tracked
separately in task #22143.
