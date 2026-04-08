# Voice Chat

Gobby provides local speech-to-text (STT) and text-to-speech (TTS) for voice conversations with your AI coding assistants through the web chat.

## Overview

The voice pipeline:

1. **You speak** -- browser VAD detects speech, records audio
2. **STT** -- Whisper (faster-whisper) transcribes audio to text locally
3. **Chat** -- transcribed text is sent as a normal chat message
4. **TTS** -- Chatterbox synthesizes the assistant's response using your cloned voice
5. **Playback** -- audio streams back over WebSocket and plays in the browser

All inference runs locally. No audio leaves your machine.

## Installation

### During `gobby install`

The installer asks whether you want voice chat. Say yes, or pass `--voice`:

```bash
gobby install --voice
```

### Manual install

```bash
# Install voice dependencies (~500MB, includes PyTorch)
uv sync --extra voice
```

## Configuration

Enable voice in your daemon config:

```yaml
voice:
  enabled: true
  tts_provider: chatterbox   # or "kokoro" for fixed voices
  tts_reference_audio: ~/.gobby/voice/reference.wav
  tts_temperature: 0.8       # sampling randomness (0.1-1.0)
  tts_device: auto            # auto, cuda, mps, cpu
```

Restart the daemon after config changes: `gobby restart`

## Voice Reference

Chatterbox clones a voice from a short reference audio clip. Place your reference at:

```
~/.gobby/voice/reference.wav
```

### Requirements

- **Duration**: 10-20 seconds (minimum 5s, sweet spot ~15s)
- **Format**: WAV (any sample rate -- internally resampled to 16/24kHz)
- **Content**: Clean speech, single speaker, consistent tone
- **Quality**: Quiet environment, minimal background noise

### Sampling from YouTube

Use yt-dlp to extract a voice sample from any video:

```bash
# Install tools
brew install yt-dlp ffmpeg

# Download full audio as WAV
yt-dlp -x --audio-format wav -o "output.wav" "https://youtube.com/watch?v=VIDEO_ID"

# Extract a specific segment (e.g., 1:30 to 1:45)
yt-dlp -x --audio-format wav \
  --postprocessor-args "ffmpeg:-ss 00:01:30 -to 00:01:45" \
  -o "~/.gobby/voice/reference.wav" "https://youtube.com/watch?v=VIDEO_ID"
```

Tips for picking a good segment:
- Choose a section with clear speech (no music, no overlapping voices)
- Avoid segments with coughing, laughing, or long pauses
- Pick a tone that matches how you want the assistant to sound

### Recording your own

Record ~15 seconds of natural speech using any tool:

```bash
# macOS Quick Recording (stop with Ctrl+C)
sox -d -r 16000 -c 1 ~/.gobby/voice/reference.wav trim 0 15

# Or use Voice Memos on macOS/iOS and export as WAV
```

## TTS Providers

### Chatterbox (default)

Zero-shot voice cloning using Resemble AI's Chatterbox Turbo model (350M params). Sub-200ms latency per sentence. Supports paralinguistic tags like `[laugh]` and `[chuckle]`.

- **Device**: Auto-detects CUDA > MPS (Apple Silicon) > CPU
- **Memory**: ~4-5GB VRAM (GPU) or ~8GB RAM (CPU/MPS)
- **Models**: Auto-downloaded from HuggingFace on first use

### Kokoro (legacy)

Fixed voices using Kokoro ONNX (82M params). Lighter weight but no voice cloning. Requires manual model download.

Set `tts_provider: kokoro` and download:
- `~/.gobby/models/kokoro-v1.0.onnx`
- `~/.gobby/models/voices-v1.0.bin`

## Usage

1. Open the web chat at `http://localhost:60887`
2. Click the microphone icon to toggle voice mode
3. Speak naturally -- VAD auto-detects speech start/end
4. The assistant's response plays back in your cloned voice
5. Barge-in: start speaking to interrupt TTS playback

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Voice not enabled" | Set `voice.enabled: true` in config, restart daemon |
| No microphone icon | Check `/api/voice/status` -- STT must be available |
| STT works but no TTS | Check reference audio exists at configured path |
| Slow first response | Model downloads on first use (~1-2GB). Subsequent calls are fast. |
| High memory usage | Try `tts_device: cpu` or reduce Whisper model size |
| MPS errors on Mac | Some ops may fall back to CPU. Set `tts_device: cpu` if unstable. |
| Voice sounds wrong | Try a cleaner, longer reference clip (15-20s of clear speech) |
