import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { VoiceStatusBar } from '../VoiceStatusBar'

describe('VoiceStatusBar', () => {
  it('shows Recording only during active PTT capture', () => {
    render(
      <VoiceStatusBar
        isListening={false}
        isSpeechDetected={false}
        isRecording={true}
        isTranscribing={false}
        voiceInputMode="ptt"
      />,
    )

    expect(screen.getByText('Recording...')).toBeTruthy()
  })

  it('keeps VAD ready copy out of Recording state', () => {
    render(
      <VoiceStatusBar
        isListening={true}
        isSpeechDetected={false}
        isRecording={true}
        isTranscribing={false}
        voiceInputMode="vad"
      />,
    )

    expect(screen.getByText('Ready — speak to send')).toBeTruthy()
    expect(screen.queryByText('Recording...')).toBeNull()
  })

  it('keeps VAD speech copy out of Recording state', () => {
    render(
      <VoiceStatusBar
        isListening={true}
        isSpeechDetected={true}
        isRecording={true}
        isTranscribing={false}
        voiceInputMode="vad"
      />,
    )

    expect(screen.getByText('Listening...')).toBeTruthy()
    expect(screen.queryByText('Recording...')).toBeNull()
  })

  it('lets transcribing win over PTT recording', () => {
    render(
      <VoiceStatusBar
        isListening={true}
        isSpeechDetected={true}
        isRecording={true}
        isTranscribing={true}
        voiceInputMode="ptt"
      />,
    )

    expect(screen.getByText('Transcribing...')).toBeTruthy()
    expect(screen.queryByText('Recording...')).toBeNull()
  })
})
