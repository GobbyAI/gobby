import { beforeEach, describe, expect, it, vi } from 'vitest'

const { load } = vi.hoisted(() => ({
  load: vi.fn<(options: { wasmPath: string }) => Promise<object>>(),
}))

vi.mock('@wterm/ghostty', () => ({
  GhosttyCore: { load },
}))

import { loadGhosttyCore } from '../ghosttyCore'

// withGobbyAnsiPalette wraps the loaded core's cell getters in place, so
// stub cores must expose them for loadGhosttyCore to resolve.
const makeCore = (id: string) => ({
  id,
  getCell: () => ({}),
  getScrollbackCell: () => ({}),
})

describe('loadGhosttyCore', () => {
  beforeEach(() => {
    load.mockReset()
  })

  it('returns a fresh core per call', async () => {
    const firstCore = makeCore('first')
    const secondCore = makeCore('second')
    load.mockResolvedValueOnce(firstCore).mockResolvedValueOnce(secondCore)

    await expect(loadGhosttyCore()).resolves.toBe(firstCore)
    await expect(loadGhosttyCore()).resolves.toBe(secondCore)

    expect(load).toHaveBeenCalledTimes(2)
    expect(load).toHaveBeenNthCalledWith(1, { wasmPath: '/wasm/ghostty-vt.wasm' })
    expect(load).toHaveBeenNthCalledWith(2, { wasmPath: '/wasm/ghostty-vt.wasm' })
  })

  it('can load after a rejected attempt', async () => {
    const recoveredCore = makeCore('recovered')
    load.mockRejectedValueOnce(new Error('wasm unavailable')).mockResolvedValueOnce(recoveredCore)

    await expect(loadGhosttyCore()).rejects.toThrow('wasm unavailable')
    await expect(loadGhosttyCore()).resolves.toBe(recoveredCore)
    expect(load).toHaveBeenCalledTimes(2)
  })
})
