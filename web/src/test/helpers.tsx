import { render, type RenderOptions } from '@testing-library/react'
import { type ReactElement } from 'react'

/**
 * Render helper that wraps components with any necessary providers.
 * Currently the app uses hooks directly (no context providers),
 * so this is a thin wrapper — extend as providers are added.
 */
export function renderWithProviders(
  ui: ReactElement,
  options?: Omit<RenderOptions, 'wrapper'>,
) {
  return render(ui, { ...options })
}

export { render }
export { screen, within, waitFor, act } from '@testing-library/react'
export { default as userEvent } from '@testing-library/user-event'
