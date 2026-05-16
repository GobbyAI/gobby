import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Heading, HeadingProvider } from '../Heading'

describe('Heading', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('warns in dev mode when no explicit level or provider is present', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    render(<Heading>Untitled Section</Heading>)

    expect(screen.getByRole('heading', { level: 1, name: 'Untitled Section' })).toBeInTheDocument()
    await waitFor(() => {
      expect(warn).toHaveBeenCalledWith(
        'Heading rendered without an explicit level or HeadingProvider; defaulting to h1.',
      )
    })
  })

  it('does not warn when a provider supplies an implicit level', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    render(
      <HeadingProvider level={3}>
        <Heading>Nested Section</Heading>
      </HeadingProvider>,
    )

    expect(screen.getByRole('heading', { level: 3, name: 'Nested Section' })).toBeInTheDocument()
    await waitFor(() => expect(warn).not.toHaveBeenCalled())
  })

  it('does not warn when the heading level is explicit', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    render(<Heading level={2}>Explicit Section</Heading>)

    expect(screen.getByRole('heading', { level: 2, name: 'Explicit Section' })).toBeInTheDocument()
    await waitFor(() => expect(warn).not.toHaveBeenCalled())
  })
})
