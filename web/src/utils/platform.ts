import { mobileTierQuery } from '../hooks/useIsMobile'

/**
 * Cached platform detection and WebGL capability probe.
 * All values computed once on module load.
 */

const ua = typeof navigator !== 'undefined' ? navigator.userAgent : ''

/** Device has touch capability plus either a mobile UA or mobile-tier viewport. */
export const IS_MOBILE_DEVICE: boolean = (() => {
  if (typeof navigator === 'undefined') return false
  const hasTouch = navigator.maxTouchPoints > 1
  const mobileUA = /Android|iPhone|iPad|iPod/i.test(ua)
  const mobileTierViewport = (
    typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia(mobileTierQuery()).matches
  )
  return hasTouch && (mobileUA || mobileTierViewport)
})()

/** Running on iOS (iPhone, iPad, iPod) including iPadOS (reports as MacIntel + touch). */
export const IS_IOS_DEVICE: boolean = (() => {
  if (typeof navigator === 'undefined') return false
  if (/iPhone|iPad|iPod/.test(ua)) return true
  // iPadOS: Safari reports "Macintosh" UA but has touch
  return /Macintosh/i.test(ua) && navigator.maxTouchPoints > 1
})()

export type WebGLTier = 'high' | 'medium' | 'low' | 'none'

export interface WebGLCapabilities {
  supported: boolean
  tier: WebGLTier
  maxTextureSize: number
}

/** Probe WebGL support and classify into performance tiers. */
export const WEBGL_CAP: WebGLCapabilities = (() => {
  if (typeof document === 'undefined') {
    return { supported: false, tier: 'none' as WebGLTier, maxTextureSize: 0 }
  }
  const canvas = document.createElement('canvas')
  const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl')
  if (!gl) {
    return { supported: false, tier: 'none' as WebGLTier, maxTextureSize: 0 }
  }
  const ctx = gl as WebGLRenderingContext
  const maxTex = ctx.getParameter(ctx.MAX_TEXTURE_SIZE) as number

  // Free the throwaway context
  const ext = ctx.getExtension('WEBGL_lose_context')
  if (ext) ext.loseContext()

  let tier: WebGLTier
  if (maxTex >= 16384) tier = 'high'
  else if (maxTex >= 8192) tier = 'medium'
  else tier = 'low'

  return { supported: true, tier, maxTextureSize: maxTex }
})()
