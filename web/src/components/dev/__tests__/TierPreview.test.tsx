import { describe, expect, it } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { TierPreview } from '../TierPreview'
import { isTierPreviewRequested } from '../tierPreviewConfig'

describe('isTierPreviewRequested', () => {
  it('detects the tier-preview query param', () => {
    expect(isTierPreviewRequested('?tier-preview')).toBe(true)
    expect(isTierPreviewRequested('?tier-preview=1&other=x')).toBe(true)
  })

  it('is off without the param', () => {
    expect(isTierPreviewRequested('')).toBe(false)
    expect(isTierPreviewRequested('?other=x')).toBe(false)
  })
})

describe('TierPreview', () => {
  it('defaults to the portrait tier with a param-free iframe src', () => {
    render(<TierPreview />)
    const frame = screen.getByTestId<HTMLIFrameElement>('tier-frame')
    expect(frame.getAttribute('src')).toBe('/')
    expect(frame.style.width).toBe('440px')
    expect(frame.style.height).toBe('956px')
    expect(screen.getByTestId('tier-size')).toHaveTextContent('440×956')
  })

  it('switches to landscape dimensions', () => {
    render(<TierPreview />)
    fireEvent.click(screen.getByRole('radio', { name: 'Landscape' }))
    const frame = screen.getByTestId<HTMLIFrameElement>('tier-frame')
    expect(frame.style.width).toBe('932px')
    expect(frame.style.height).toBe('430px')
    expect(screen.getByTestId('tier-size')).toHaveTextContent('932×430')
  })

  it('fills the stage on the desktop tier', () => {
    render(<TierPreview />)
    fireEvent.click(screen.getByRole('radio', { name: 'Desktop' }))
    const frame = screen.getByTestId<HTMLIFrameElement>('tier-frame')
    expect(frame.style.width).toBe('100%')
    expect(frame.style.height).toBe('100%')
    expect(screen.getByTestId('tier-size')).toHaveTextContent('fill')
  })
})
