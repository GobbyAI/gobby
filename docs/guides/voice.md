# Voice Chat

Gobby provides local speech-to-text (STT) and provider-based text-to-speech (TTS)
for voice conversations with your AI coding assistants through the web chat.

## Overview

The voice pipeline:

1. **You speak** - browser VAD detects speech and records audio
2. **STT** - Whisper (`faster-whisper`) transcribes audio locally
3. **Chat** - the transcript is sent as a normal chat message
4. **TTS** - the configured provider synthesizes the assistant response
5. **Playback** - PCM audio streams back over WebSocket and plays in the browser

All inference runs locally. No audio leaves your machine.

## Installation

### During `gobby install`

The installer asks whether you want voice chat. Say yes, or pass `--voice`:

```bash
gobby install --voice
```

### Voice dependencies

```bash
uv sync --extra voice
```

Installs `faster-whisper` (STT) and `chatterbox-tts` (TTS).

## Configuration

Enable voice in your daemon config:

```yaml
voice:
  enabled: true
  tts_provider: chatterbox
```

Restart the daemon after config changes: `gobby restart`

## Reference Audio

Chatterbox performs zero-shot voice cloning from a short reference clip:

```yaml
voice:
  tts_reference_audio: ~/.gobby/voice/reference.wav
```

Guidelines:

- Duration: 10-20 seconds works well; 5+ seconds is the bare minimum
- Format: WAV
- Content: clean speech, one speaker, consistent tone
- Quality: quiet room, minimal noise, no overlapping voices

### Optional `tts_reference_text`

Providers that support higher-fidelity cloning can use the transcript of the
reference clip. Chatterbox ignores it, but the field is preserved for
forward-compatible provider additions:

```yaml
voice:
  tts_reference_audio: ~/.gobby/voice/reference.wav
  tts_reference_text: "The exact transcript of that reference clip."
```

Behavior:

- Optional for all providers
- Ignored if missing
- Ignored by providers that do not support it

## TTS Providers

### Chatterbox

Zero-shot voice cloning with a short reference clip.

```yaml
voice:
  enabled: true
  tts_provider: chatterbox
  tts_reference_audio: ~/.gobby/voice/reference.wav
  tts_temperature: 0.55
  tts_device: auto
```

Notes:

- Uses `tts_reference_audio`
- Ignores `tts_reference_text`
- Output is 24kHz
- `tts_device` accepts `auto`, `cuda`, `mps`, `cpu`

## Usage

1. Open the web chat at `http://localhost:60887`
2. Click the microphone icon to toggle voice mode
3. Speak naturally - VAD auto-detects speech start/end
4. The assistant response plays back through the configured TTS provider
5. Barge-in: start speaking to interrupt TTS playback

## Troubleshooting

| Issue | What to check |
|-------|---------------|
| "Voice not enabled" | Set `voice.enabled: true` and restart the daemon |
| No microphone icon | Check `/api/voice/status`; STT must be available |
| STT works but TTS does not | Check `tts_provider`, provider install status, and provider-specific readiness in `/api/voice/status` |
| Cloning sounds wrong | Use a cleaner 8-15s clip, prefer mono |
| Chatterbox unstable on your machine | Try `tts_device: cpu` |

Provider status is reported through `/api/voice/status`, including:

- `tts_provider`
- `tts_available`
- `tts_reason`
- `tts_backend_kind`
- `tts_capabilities`

These fields are the quickest way to see whether the active provider supports
reference audio, reference text, or streaming behavior.
