const DATA_IMAGE_RE = /^data:image\/[a-z0-9.+-]+(?:;[a-z0-9.+-]+=[^;,]+)*(?:;base64)?,/i
const IMAGE_MEDIA_TYPE_RE = /^image\/[a-z0-9.+-]+$/i
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i
const UNSAFE_URL_CHARS_RE = /[\s<>"'\\]/
const WRAPPER_KEYS = ['output', 'result', 'content'] as const
const IMAGE_TYPES = new Set(['image', 'input_image', 'output_image', 'image_url'])
const IMAGE_PATH_RE = /\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?:[?#]|$)/i
const MAX_DEPTH = 12

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function tryParseJson(value: string): unknown {
  const trimmed = value.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null

  try {
    return JSON.parse(trimmed)
  } catch {
    return null
  }
}

function hasUnsafeUrlChars(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    if (code <= 0x1f || code === 0x7f) return true
  }
  return UNSAFE_URL_CHARS_RE.test(value)
}

export function isSafeImageSrc(src: string): boolean {
  const trimmed = src.trim()
  if (!trimmed || hasUnsafeUrlChars(trimmed)) return false

  if (DATA_IMAGE_RE.test(trimmed)) {
    return true
  }

  if (SCHEME_RE.test(trimmed)) {
    return trimmed.toLowerCase().startsWith('https://')
  }

  if (trimmed.startsWith('//')) {
    return false
  }

  if (trimmed.startsWith('/')) {
    return true
  }

  return trimmed.startsWith('./') || trimmed.startsWith('../') || IMAGE_PATH_RE.test(trimmed)
}

function safeImageSrc(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return isSafeImageSrc(trimmed) ? trimmed : null
}

function safeImageUrl(value: unknown): string | null {
  if (typeof value === 'string') return safeImageSrc(value)
  if (isRecord(value)) return safeImageSrc(value.url)
  return null
}

function extractBase64Source(source: unknown): string | null {
  if (!isRecord(source)) return null
  if (
    (source.type !== undefined && source.type !== 'base64') ||
    typeof source.media_type !== 'string' ||
    typeof source.data !== 'string' ||
    !IMAGE_MEDIA_TYPE_RE.test(source.media_type)
  ) {
    return null
  }

  const src = `data:${source.media_type};base64,${source.data}`
  return safeImageSrc(src)
}

function extractImageSrcInner(
  value: unknown,
  seen: WeakSet<object>,
  depth: number,
): string | null {
  if (depth > MAX_DEPTH) return null

  const directSrc = safeImageSrc(value)
  if (directSrc) return directSrc

  if (typeof value === 'string') {
    const parsed = tryParseJson(value)
    return parsed == null ? null : extractImageSrcInner(parsed, seen, depth + 1)
  }

  if (Array.isArray(value)) {
    if (seen.has(value)) return null
    seen.add(value)
    for (const item of value) {
      const found = extractImageSrcInner(item, seen, depth + 1)
      if (found) return found
    }
    return null
  }

  if (!isRecord(value)) return null
  if (seen.has(value)) return null
  seen.add(value)

  if (typeof value.type === 'string' && IMAGE_TYPES.has(value.type)) {
    const sourceSrc = extractBase64Source(value.source)
    if (sourceSrc) return sourceSrc

    const sourceUrlSrc = safeImageUrl(value.source)
    if (sourceUrlSrc) return sourceUrlSrc

    const imageUrlSrc = safeImageUrl(value.image_url)
    if (imageUrlSrc) return imageUrlSrc

    const urlSrc = safeImageUrl(value.url)
    if (urlSrc) return urlSrc
  }

  for (const key of WRAPPER_KEYS) {
    if (key in value) {
      const found = extractImageSrcInner(value[key], seen, depth + 1)
      if (found) return found
    }
  }

  return null
}

export function extractImageSrc(value: unknown): string | null {
  return extractImageSrcInner(value, new WeakSet(), 0)
}
