import { describe, expect, it } from 'vitest'

import { extractImageSrc, isSafeImageSrc } from '../imageSources'

describe('isSafeImageSrc', () => {
  it('allows data, HTTPS, same-origin, and explicit relative image sources', () => {
    expect(isSafeImageSrc('data:image/png;base64,abc')).toBe(true)
    expect(isSafeImageSrc('https://example.test/image.png')).toBe(true)
    expect(isSafeImageSrc('/api/files/image?id=1')).toBe(true)
    expect(isSafeImageSrc('./image.png')).toBe(true)
    expect(isSafeImageSrc('../image.png')).toBe(true)
  })

  it('rejects unsafe or ambiguous image source strings', () => {
    expect(isSafeImageSrc('http://example.test/image.png')).toBe(false)
    expect(isSafeImageSrc('//example.test/image.png')).toBe(false)
    expect(isSafeImageSrc('javascript:alert(1)')).toBe(false)
    expect(isSafeImageSrc('image.png')).toBe(false)
    expect(isSafeImageSrc('/image.png" onerror="alert(1)')).toBe(false)
  })
})

describe('extractImageSrc', () => {
  it('extracts base64 content blocks with or without an explicit source type', () => {
    expect(
      extractImageSrc({
        type: 'image',
        source: { media_type: 'image/png', data: 'abc' },
      }),
    ).toBe('data:image/png;base64,abc')

    expect(
      extractImageSrc({
        type: 'image',
        source: { type: 'base64', media_type: 'image/jpeg', data: '/9j/4AAQ==' },
      }),
    ).toBe('data:image/jpeg;base64,/9j/4AAQ==')
  })

  it('extracts Codex image URL wrappers', () => {
    expect(
      extractImageSrc({
        content: {
          type: 'output_image',
          image_url: { url: 'https://example.test/generated.png' },
        },
      }),
    ).toBe('https://example.test/generated.png')
  })

  it('extracts nested image blocks from arrays and JSON strings', () => {
    expect(
      extractImageSrc([
        { type: 'text', text: 'result' },
        { type: 'image_url', url: '/api/files/generated.webp' },
      ]),
    ).toBe('/api/files/generated.webp')

    expect(
      extractImageSrc(
        JSON.stringify({
          output: { type: 'output_image', image_url: './generated.png' },
        }),
      ),
    ).toBe('./generated.png')
  })

  it('rejects malformed, unsafe, or cyclic image wrappers', () => {
    expect(extractImageSrc('plain-token')).toBeNull()
    expect(
      extractImageSrc({
        type: 'image',
        source: { media_type: 'text/plain', data: 'abc' },
      }),
    ).toBeNull()
    expect(
      extractImageSrc({
        type: 'output_image',
        image_url: 'http://example.test/generated.png',
      }),
    ).toBeNull()

    const cyclic: Record<string, unknown> = {}
    cyclic.content = cyclic
    expect(extractImageSrc(cyclic)).toBeNull()
  })
})
