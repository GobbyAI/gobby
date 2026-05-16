import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ThemeToggle } from '../ThemeToggle'

function setResolvedTheme(theme: 'dark' | 'light') {
  document.documentElement.setAttribute('data-theme', theme)
}

afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
})

describe('ThemeToggle', () => {
  it('shows the sun affordance in dark mode and switches to light on click', () => {
    setResolvedTheme('dark')
    const onThemeChange = vi.fn()
    render(<ThemeToggle theme="dark" onThemeChange={onThemeChange} />)

    const button = screen.getByRole('button', { name: 'Switch to light theme' })
    expect(button).toHaveClass('btn', 'btn-accent', 'btn-sm')

    fireEvent.click(button)
    expect(onThemeChange).toHaveBeenCalledWith('light')
  })

  it('shows the moon affordance in light mode and switches to dark on click', () => {
    setResolvedTheme('light')
    const onThemeChange = vi.fn()
    render(<ThemeToggle theme="light" onThemeChange={onThemeChange} />)

    const button = screen.getByRole('button', { name: 'Switch to dark theme' })
    fireEvent.click(button)
    expect(onThemeChange).toHaveBeenCalledWith('dark')
  })

  it('resolves a "system" setting to the on-screen theme', () => {
    setResolvedTheme('light')
    const onThemeChange = vi.fn()
    render(<ThemeToggle theme="system" onThemeChange={onThemeChange} />)

    fireEvent.click(screen.getByRole('button', { name: 'Switch to dark theme' }))
    expect(onThemeChange).toHaveBeenCalledWith('dark')
  })

  it('does not invoke the callback when disabled', () => {
    setResolvedTheme('dark')
    const onThemeChange = vi.fn()
    render(<ThemeToggle theme="dark" onThemeChange={onThemeChange} disabled />)

    const button = screen.getByRole('button', { name: 'Switch to light theme' })
    expect(button).toBeDisabled()

    fireEvent.click(button)
    expect(onThemeChange).not.toHaveBeenCalled()
  })
})
