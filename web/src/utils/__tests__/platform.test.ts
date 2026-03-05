import { describe, it, expect, vi, beforeEach } from 'vitest'

const defaultUA = navigator.userAgent
const defaultTouchPoints = navigator.maxTouchPoints
const defaultInnerWidth = window.innerWidth

beforeEach(() => {
  vi.resetModules()
  vi.restoreAllMocks()
  Object.defineProperty(navigator, 'userAgent', { value: defaultUA, configurable: true })
  Object.defineProperty(navigator, 'maxTouchPoints', { value: defaultTouchPoints, configurable: true })
  Object.defineProperty(window, 'innerWidth', { value: defaultInnerWidth, configurable: true })
})

describe('IS_MOBILE', () => {
  it('returns false in default jsdom (no touch, desktop UA)', async () => {
    const { IS_MOBILE } = await import('../platform')
    expect(IS_MOBILE).toBe(false)
  })

  it('returns true for touch + mobile UA', async () => {
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5, configurable: true })
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
      configurable: true,
    })
    const { IS_MOBILE } = await import('../platform')
    expect(IS_MOBILE).toBe(true)
  })

  it('returns true for touch + narrow viewport', async () => {
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5, configurable: true })
    Object.defineProperty(window, 'innerWidth', { value: 375, configurable: true })
    const { IS_MOBILE } = await import('../platform')
    expect(IS_MOBILE).toBe(true)
  })

  it('returns false for touch without mobile UA or narrow viewport', async () => {
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5, configurable: true })
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (Macintosh; Intel Mac OS X)',
      configurable: true,
    })
    Object.defineProperty(window, 'innerWidth', { value: 1920, configurable: true })
    const { IS_MOBILE } = await import('../platform')
    expect(IS_MOBILE).toBe(false)
  })
})

describe('IS_IOS', () => {
  it('returns false for desktop UA', async () => {
    const { IS_IOS } = await import('../platform')
    expect(IS_IOS).toBe(false)
  })

  it('returns true for iPhone UA', async () => {
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
      configurable: true,
    })
    const { IS_IOS } = await import('../platform')
    expect(IS_IOS).toBe(true)
  })

  it('returns true for iPad UA', async () => {
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)',
      configurable: true,
    })
    const { IS_IOS } = await import('../platform')
    expect(IS_IOS).toBe(true)
  })

  it('returns true for iPadOS (Macintosh UA + touch)', async () => {
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
      configurable: true,
    })
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5, configurable: true })
    const { IS_IOS } = await import('../platform')
    expect(IS_IOS).toBe(true)
  })
})

describe('WEBGL_CAP', () => {
  it('reports supported in jsdom with mocked WebGL', async () => {
    // jsdom doesn't support canvas/WebGL, so we get the no-gl path
    const { WEBGL_CAP } = await import('../platform')
    // In jsdom, getContext returns null, so we expect no support
    expect(WEBGL_CAP).toEqual({
      supported: false,
      tier: 'none',
      maxTextureSize: 0,
    })
  })

  it('reports correct tier for high-end GPU', async () => {
    const mockCtx = {
      getParameter: vi.fn().mockReturnValue(16384),
      getExtension: vi.fn().mockReturnValue({ loseContext: vi.fn() }),
      MAX_TEXTURE_SIZE: 0x0d33,
    }
    vi.spyOn(document, 'createElement').mockReturnValue({
      getContext: vi.fn().mockReturnValue(mockCtx),
    } as unknown as HTMLCanvasElement)

    const { WEBGL_CAP } = await import('../platform')
    expect(WEBGL_CAP).toEqual({
      supported: true,
      tier: 'high',
      maxTextureSize: 16384,
    })
  })

  it('reports medium tier for 8192 texture size', async () => {
    const mockCtx = {
      getParameter: vi.fn().mockReturnValue(8192),
      getExtension: vi.fn().mockReturnValue(null),
      MAX_TEXTURE_SIZE: 0x0d33,
    }
    vi.spyOn(document, 'createElement').mockReturnValue({
      getContext: vi.fn().mockReturnValue(mockCtx),
    } as unknown as HTMLCanvasElement)

    const { WEBGL_CAP } = await import('../platform')
    expect(WEBGL_CAP).toEqual({
      supported: true,
      tier: 'medium',
      maxTextureSize: 8192,
    })
  })

  it('reports low tier for small texture size', async () => {
    const mockCtx = {
      getParameter: vi.fn().mockReturnValue(4096),
      getExtension: vi.fn().mockReturnValue(null),
      MAX_TEXTURE_SIZE: 0x0d33,
    }
    vi.spyOn(document, 'createElement').mockReturnValue({
      getContext: vi.fn().mockReturnValue(mockCtx),
    } as unknown as HTMLCanvasElement)

    const { WEBGL_CAP } = await import('../platform')
    expect(WEBGL_CAP).toEqual({
      supported: true,
      tier: 'low',
      maxTextureSize: 4096,
    })
  })
})
