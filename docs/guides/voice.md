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

### Baseline voice dependencies

```bash
# Installs Whisper STT plus the legacy built-in Chatterbox/Kokoro TTS providers.
uv sync --extra voice
```

`uv sync --extra voice` does **not** install VoxCPM, even though VoxCPM is the
default provider in config. Install it separately or use the interactive installer.

### VoxCPM provider

Install `voxcpm` manually if you plan to use `tts_provider: voxcpm`.

```bash
uv pip install voxcpm
```

Notes:

- The current Gobby integration treats VoxCPM as an optional provider, not a baseline dependency.
- Upstream VoxCPM currently documents embedded usage around Python `<3.13`. If it does not
  install cleanly into the daemon runtime on your machine, keep using another provider until a
  dedicated external runtime path is added.

## Configuration

Enable voice in your daemon config:

```yaml
voice:
  enabled: true
  tts_provider: voxcpm
```

Restart the daemon after config changes: `gobby restart`

## Reference Audio

The simple cloning workflow stays the same across providers that support it:

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

Some providers can also use the transcript of the reference clip:

```yaml
voice:
  tts_reference_audio: ~/.gobby/voice/reference.wav
  tts_reference_text: "The exact transcript of that reference clip."
```

Behavior:

- Optional for all providers
- Ignored if missing
- Ignored by providers that do not support it
- Used by VoxCPM to switch from simple reference-audio cloning to a higher-fidelity prompt-audio mode

If you do not want to transcribe the clip, leave it unset. Basic cloning still works.

## TTS Providers

### VoxCPM (default)

Reference-audio cloning with optional `reference_text` for higher-fidelity continuation-style cloning.

```yaml
voice:
  enabled: true
  tts_provider: voxcpm
  tts_reference_audio: ~/.gobby/voice/reference.wav
  tts_reference_text: "Optional transcript of the reference clip."
  tts_voxcpm_model: openbmb/VoxCPM2
  tts_voxcpm_cfg_value: 2.0
  tts_voxcpm_inference_timesteps: 10
  tts_voxcpm_load_denoiser: false
  tts_voxcpm_denoise: false
  tts_voxcpm_local_files_only: false
  tts_voxcpm_optimize: true
```

Notes:

- Default provider for new voice configs
- `tts_reference_audio` alone enables normal cloning
- Adding `tts_reference_text` lets the provider reuse the same clip as prompt audio for better similarity
- Output is typically 48kHz
- Embedded VoxCPM currently auto-selects its runtime device; `tts_device` is not enforced
- VoxCPM is not installed by `uv sync --extra voice`

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

- Good fallback if you want the older "drop in a WAV and go" workflow
- Uses `tts_reference_audio`
- Ignores `tts_reference_text`

### Kokoro

Fixed voices through Kokoro ONNX. Lighter weight, but not a voice-cloning provider.

```yaml
voice:
  enabled: true
  tts_provider: kokoro
  tts_voice: af_heart
  tts_speed: 1.0
  tts_language: en-us
  tts_model_path: ~/.gobby/models/kokoro-v1.0.onnx
  tts_voices_path: ~/.gobby/models/voices-v1.0.bin
```

Notes:

- Does not use `tts_reference_audio`
- Does not use `tts_reference_text`
- Requires local model files

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
| Cloning sounds wrong | Use a cleaner or longer reference clip |
| `tts_reference_text` has no effect | The active provider may ignore it, or the field may be unset/blank |
| VoxCPM unavailable | Confirm `voxcpm` is installed in the daemon runtime and supported on your platform/Python |
| Kokoro unavailable | Check `tts_model_path` and `tts_voices_path` |
| Chatterbox unstable on your machine | Try `tts_device: cpu` or switch providers |

Provider status is reported through `/api/voice/status`, including:

- `tts_provider`
- `tts_available`
- `tts_reason`
- `tts_backend_kind`
- `tts_capabilities`

These fields are the quickest way to see whether the active provider supports
reference audio, reference text, or streaming behavior.
