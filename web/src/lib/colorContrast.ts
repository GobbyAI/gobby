/**
 * WCAG contrast math for OKLCH design tokens.
 *
 * The Gobby palette is authored entirely in OKLCH (`oklch(L% C H)`), but WCAG
 * 2.2 contrast is defined on sRGB relative luminance. This module converts an
 * OKLCH string to relative luminance and computes the contrast ratio between
 * two such strings, so the `{ dark, light }` token pairs can assert their AA
 * floor in unit tests with no color-library dependency.
 *
 * The OKLab → linear-sRGB matrices are from Björn Ottosson's reference
 * implementation (https://bottosson.github.io/posts/oklab/, public domain).
 * Linear sRGB is already the linearized space WCAG luminance is defined on, so
 * the channels are gamut-clamped and weighted directly — no extra transfer
 * function is applied.
 */

export interface Oklch {
  /** Lightness, 0–1 (an `L%` token value is divided by 100). */
  l: number
  /** Chroma, roughly 0–0.4. */
  c: number
  /** Hue in degrees, 0–360. */
  h: number
}

const OKLCH_PATTERN = /^oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)\s*\)$/i

/** Parse an `oklch(L% C H)` string. Throws on malformed input. */
export function parseOklch(value: string): Oklch {
  const match = OKLCH_PATTERN.exec(value.trim())
  if (!match) {
    throw new Error(`Not a parseable oklch() string: ${value}`)
  }
  return {
    l: Number(match[1]) / 100,
    c: Number(match[2]),
    h: Number(match[3]),
  }
}

const clamp01 = (n: number): number => Math.min(1, Math.max(0, n))

/** WCAG relative luminance (0–1) of an OKLCH color string. */
export function relativeLuminance(value: string): number {
  const { l: lightness, c, h } = parseOklch(value)
  const hRad = (h * Math.PI) / 180
  const a = c * Math.cos(hRad)
  const b = c * Math.sin(hRad)

  // OKLab → non-linear LMS → linear LMS
  const lRoot = lightness + 0.3963377774 * a + 0.2158037573 * b
  const mRoot = lightness - 0.1055613458 * a - 0.0638541728 * b
  const sRoot = lightness - 0.0894841775 * a - 1.291485548 * b
  const l = lRoot ** 3
  const m = mRoot ** 3
  const s = sRoot ** 3

  // linear LMS → linear sRGB
  const r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
  const g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
  const blue = -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s

  return 0.2126 * clamp01(r) + 0.7152 * clamp01(g) + 0.0722 * clamp01(blue)
}

/**
 * WCAG 2.x contrast ratio between two OKLCH color strings. Order-independent;
 * the result is in [1, 21].
 */
export function contrastRatio(a: string, b: string): number {
  const lumA = relativeLuminance(a)
  const lumB = relativeLuminance(b)
  const lighter = Math.max(lumA, lumB)
  const darker = Math.min(lumA, lumB)
  return (lighter + 0.05) / (darker + 0.05)
}
