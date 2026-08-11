import { useState, useEffect } from 'react'

const MOBILE_MAX_WIDTH_TOKEN = '--breakpoint-mobile-max-width'
const MOBILE_MAX_HEIGHT_TOKEN = '--breakpoint-mobile-max-height'
const DEFAULT_MOBILE_MAX_WIDTH = 767
const DEFAULT_MOBILE_MAX_HEIGHT = 500
const warnedInvalidTokens = new Set<string>()

function readPixelToken(styles: CSSStyleDeclaration, name: string, fallback: number): number {
  const rawValue = styles.getPropertyValue(name).trim()
  const match = /^(\d+(?:\.\d+)?)px$/.exec(rawValue)
  const value = match ? Number(match[1]) : Number.NaN
  if (Number.isFinite(value) && value >= 0) return value

  const warningKey = `${name}:${rawValue}`
  if (!warnedInvalidTokens.has(warningKey)) {
    warnedInvalidTokens.add(warningKey)
    console.warn(
      `Invalid responsive tier token ${name}=${JSON.stringify(rawValue)}; using ${fallback}px`,
    )
  }
  return fallback
}

/** Build the geometry-only query shared by every JavaScript tier consumer. */
export function mobileTierQuery(): string {
  if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') {
    return `(max-width: ${DEFAULT_MOBILE_MAX_WIDTH}px), (max-height: ${DEFAULT_MOBILE_MAX_HEIGHT}px)`
  }

  const styles = getComputedStyle(document.documentElement)
  const maxWidth = readPixelToken(styles, MOBILE_MAX_WIDTH_TOKEN, DEFAULT_MOBILE_MAX_WIDTH)
  const maxHeight = readPixelToken(styles, MOBILE_MAX_HEIGHT_TOKEN, DEFAULT_MOBILE_MAX_HEIGHT)
  return `(max-width: ${maxWidth}px), (max-height: ${maxHeight}px)`
}

function createMobileTierMediaQuery(): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  return window.matchMedia(mobileTierQuery())
}

/**
 * Reactive hook that returns true in the authored mobile viewport tier.
 * Uses matchMedia for efficient, debounce-free updates.
 */
export function useIsMobile(): boolean {
  const [mediaQuery] = useState<MediaQueryList | null>(createMobileTierMediaQuery)
  const [isMobile, setIsMobile] = useState(mediaQuery?.matches ?? false)

  useEffect(() => {
    if (!mediaQuery) return
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches)
    mediaQuery.addEventListener('change', handler)
    return () => mediaQuery.removeEventListener('change', handler)
  }, [mediaQuery])

  return isMobile
}

function createCoarsePointerMediaQuery(): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  return window.matchMedia('(pointer: coarse)')
}

/**
 * Reactive hook that returns true when the primary pointer is coarse (touch).
 * Keyboard behavior (e.g. Enter-to-submit) must key off input modality, not
 * the viewport tier: a short desktop window sits in the mobile tier but still
 * has a hardware keyboard.
 */
export function useCoarsePointer(): boolean {
  const [mediaQuery] = useState<MediaQueryList | null>(createCoarsePointerMediaQuery)
  const [coarse, setCoarse] = useState(mediaQuery?.matches ?? false)

  useEffect(() => {
    if (!mediaQuery) return
    const handler = (e: MediaQueryListEvent) => setCoarse(e.matches)
    mediaQuery.addEventListener('change', handler)
    return () => mediaQuery.removeEventListener('change', handler)
  }, [mediaQuery])

  return coarse
}
