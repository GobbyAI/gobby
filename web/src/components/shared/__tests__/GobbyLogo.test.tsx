import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { GobbyLogo } from '../GobbyLogo'

describe('GobbyLogo', () => {
  it('renders a theme-aware logo element with the default fixed size', () => {
    const { container } = render(<GobbyLogo />)

    const logo = screen.getByRole('img', { name: 'Gobby logo' })
    expect(logo).toHaveClass('gobby-logo')
    expect(logo).toHaveAttribute('style', expect.stringContaining('--gobby-logo-size: 20px'))
    expect(container.querySelector('img')).toBeNull()
  })

  it('merges custom class names and size values', () => {
    render(<GobbyLogo label="App logo" size="2rem" className="rounded" />)

    const logo = screen.getByRole('img', { name: 'App logo' })
    expect(logo).toHaveClass('gobby-logo', 'rounded')
    expect(logo).toHaveAttribute('style', expect.stringContaining('--gobby-logo-size: 2rem'))
  })
})
