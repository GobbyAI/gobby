import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ATTACHMENT_DELETE_TIMEOUT_MS,
  ATTACHMENT_UPLOAD_TIMEOUT_MS,
  MAX_ATTACHMENT_SIZE_BYTES,
  deleteChatAttachment,
  formatAttachmentSize,
  uploadChatAttachment,
} from '../chatAttachments'

class FakeUpload {
  onprogress: ((event: ProgressEvent<XMLHttpRequestEventTarget>) => void) | null = null
}

class FakeXMLHttpRequest {
  static instances: FakeXMLHttpRequest[] = []

  upload = new FakeUpload()
  timeout = 0
  withCredentials = false
  status = 0
  statusText = ''
  responseText = ''
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  ontimeout: (() => void) | null = null
  method: string | null = null
  url: string | null = null
  body: Document | XMLHttpRequestBodyInit | null = null

  constructor() {
    FakeXMLHttpRequest.instances.push(this)
  }

  open(method: string, url: string) {
    this.method = method
    this.url = url
  }

  send(body?: Document | XMLHttpRequestBodyInit | null) {
    this.body = body ?? null
  }

  abort() {
    this.onabort?.()
  }
}

const originalXMLHttpRequest = globalThis.XMLHttpRequest

afterEach(() => {
  vi.useRealTimers()
  globalThis.XMLHttpRequest = originalXMLHttpRequest
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  FakeXMLHttpRequest.instances = []
})

describe('uploadChatAttachment', () => {
  it('rejects oversized files before creating an XHR', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest
    const file = new File(['x'], 'huge.bin')
    Object.defineProperty(file, 'size', { value: MAX_ATTACHMENT_SIZE_BYTES + 1 })

    const upload = uploadChatAttachment(file)

    await expect(upload.promise).rejects.toThrow('Attachment exceeds 95.4 MB limit')
    expect(FakeXMLHttpRequest.instances).toHaveLength(0)
  })

  it('times out uploads after ten minutes and clears progress', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest
    const onProgress = vi.fn()

    const upload = uploadChatAttachment(new File(['hello'], 'note.txt'), { onProgress })
    const xhr = FakeXMLHttpRequest.instances[0]

    xhr.upload.onprogress?.(
      new ProgressEvent('progress', {
        lengthComputable: true,
        loaded: 1,
        total: 2,
      }) as ProgressEvent<XMLHttpRequestEventTarget>,
    )
    const rejection = expect(upload.promise).rejects.toThrow('Attachment upload timed out')
    xhr.ontimeout?.()

    await rejection
    expect(xhr.timeout).toBe(ATTACHMENT_UPLOAD_TIMEOUT_MS)
    expect(onProgress).toHaveBeenNthCalledWith(1, 0.5)
    expect(onProgress).toHaveBeenLastCalledWith(null)
  })

  it('rejects invalid upload JSON responses', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest

    const upload = uploadChatAttachment(new File(['hello'], 'note.txt'))
    const xhr = FakeXMLHttpRequest.instances[0]
    xhr.status = 200
    xhr.responseText = '{bad'
    const rejection = expect(upload.promise).rejects.toThrow('Attachment upload returned invalid JSON')
    xhr.onload?.()

    await rejection
  })

  it('resolves successful upload payloads with normalized content URLs', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest

    const upload = uploadChatAttachment(new File(['hello'], 'note.txt'))
    const xhr = FakeXMLHttpRequest.instances[0]
    xhr.status = 201
    xhr.responseText = JSON.stringify({
      id: 'att-1',
      project_id: 'proj-1',
      filename: 'note.txt',
      mime_type: 'text/plain',
      size_bytes: 5,
      content_url: '/api/chat/attachments/att-1/content',
    })
    xhr.onload?.()

    await expect(upload.promise).resolves.toMatchObject({
      id: 'att-1',
      filename: 'note.txt',
      content_url: '/api/chat/attachments/att-1/content',
    })
  })

  it('normalizes same-origin absolute content URLs to paths', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest

    const upload = uploadChatAttachment(new File(['hello'], 'note.txt'))
    const xhr = FakeXMLHttpRequest.instances[0]
    xhr.status = 201
    xhr.responseText = JSON.stringify({
      id: 'att-1',
      project_id: 'proj-1',
      filename: 'note.txt',
      mime_type: 'text/plain',
      size_bytes: 5,
      content_url: `${window.location.origin}/api/chat/attachments/att-1/content?download=1`,
    })
    xhr.onload?.()

    await expect(upload.promise).resolves.toMatchObject({
      content_url: '/api/chat/attachments/att-1/content?download=1',
    })
  })

  it('returns an abort handle that cancels the XHR', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest
    const onProgress = vi.fn()

    const upload = uploadChatAttachment(new File(['hello'], 'note.txt'), { onProgress })
    const xhr = FakeXMLHttpRequest.instances[0]
    const abort = vi.fn(() => xhr.onabort?.())
    xhr.abort = abort
    const rejection = expect(upload.promise).rejects.toThrow('Attachment upload canceled')

    upload.abort()

    await rejection
    expect(abort).toHaveBeenCalled()
    expect(onProgress).toHaveBeenLastCalledWith(null)
  })

  it('rejects upload JSON with an invalid attachment shape', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest

    const upload = uploadChatAttachment(new File(['hello'], 'note.txt'))
    const xhr = FakeXMLHttpRequest.instances[0]
    xhr.status = 200
    xhr.responseText = JSON.stringify({ id: 'att-1' })
    const rejection = expect(upload.promise).rejects.toThrow(
      'Attachment upload response field project_id must be a string',
    )
    xhr.onload?.()

    await rejection
  })
})

describe('deleteChatAttachment', () => {
  it('resolves successful delete responses', async () => {
    const response = new Response(null, { status: 204 })
    const fetchMock = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(deleteChatAttachment('att-1')).resolves.toBe(response)
  })

  it('encodes IDs and throws on non-OK responses with the response body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('delete denied', {
        status: 409,
        statusText: 'Conflict',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(deleteChatAttachment('id/with slash')).rejects.toThrow('delete denied')

    expect(fetchMock).toHaveBeenCalledWith('/api/chat/attachments/id%2Fwith%20slash', {
      method: 'DELETE',
      credentials: 'include',
      signal: expect.any(AbortSignal),
    })
  })

  it('throws a timeout-specific error when delete aborts', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn((_url: string | URL | Request, init?: RequestInit) => (
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      })
    ))
    vi.stubGlobal('fetch', fetchMock)

    const result = expect(deleteChatAttachment('att-1')).rejects.toThrow(
      'Attachment delete timed out',
    )
    await vi.advanceTimersByTimeAsync(ATTACHMENT_DELETE_TIMEOUT_MS)
    await result
    vi.useRealTimers()
  })
})

describe('formatAttachmentSize', () => {
  it('clamps non-positive byte counts to zero bytes', () => {
    expect(formatAttachmentSize(0)).toBe('0 B')
    expect(formatAttachmentSize(-1)).toBe('0 B')
  })

  it('formats KB, MB, and GB byte counts', () => {
    expect(formatAttachmentSize(1536)).toBe('1.5 KB')
    expect(formatAttachmentSize(2 * 1024 * 1024)).toBe('2.0 MB')
    expect(formatAttachmentSize(3 * 1024 * 1024 * 1024)).toBe('3.0 GB')
  })
})
