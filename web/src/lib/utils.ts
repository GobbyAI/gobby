import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// Resolves a CSS custom property to a concrete RGB color string at runtime.
// Required for three.js / canvas consumers that can't read CSS vars themselves
// and for older parsers that don't grok oklch()/lab()/color(). The canvas
// getImageData round-trip forces conversion to actual sRGB. Cached per
// (var, alpha); cache flushes on theme attribute changes.

const cssVarCache = new Map<string, string>()
let cssVarObserver: MutationObserver | null = null
let probeCtx: CanvasRenderingContext2D | null | undefined

function ensureCssVarObserver() {
  if (cssVarObserver || typeof document === 'undefined') return
  cssVarObserver = new MutationObserver(() => cssVarCache.clear())
  cssVarObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme', 'class', 'style'],
  })
}

function getProbeCtx(): CanvasRenderingContext2D | null {
  if (probeCtx !== undefined) return probeCtx
  try {
    const canvas = document.createElement('canvas')
    canvas.width = 1
    canvas.height = 1
    probeCtx = canvas.getContext('2d', { willReadFrequently: true }) ?? null
  } catch {
    probeCtx = null
  }
  return probeCtx
}

export function resolveCssVar(varName: string, alpha?: number): string {
  if (typeof document === 'undefined') return ''
  const key = `${varName}@${alpha ?? 1}`
  const cached = cssVarCache.get(key)
  if (cached !== undefined) return cached
  ensureCssVarObserver()

  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim()
  if (!raw) {
    cssVarCache.set(key, '')
    return ''
  }

  const ctx = getProbeCtx()
  let result = raw
  if (ctx) {
    try {
      ctx.clearRect(0, 0, 1, 1)
      ctx.fillStyle = raw
      ctx.fillRect(0, 0, 1, 1)
      const data = ctx.getImageData(0, 0, 1, 1).data
      const r = data[0]
      const g = data[1]
      const b = data[2]
      const baseA = data[3] / 255
      const finalA = alpha !== undefined ? alpha : baseA
      result = finalA >= 1
        ? `rgb(${r}, ${g}, ${b})`
        : `rgba(${r}, ${g}, ${b}, ${finalA})`
    } catch {
      // fall through with raw value
    }
  }

  cssVarCache.set(key, result)
  return result
}
