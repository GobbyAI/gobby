import { afterEach, describe, expect, it, vi } from 'vitest'

import {
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
}

const originalXMLHttpRequest = globalThis.XMLHttpRequest

afterEach(() => {
  globalThis.XMLHttpRequest = originalXMLHttpRequest
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  FakeXMLHttpRequest.instances = []
})

describe('uploadChatAttachment', () => {
  it('times out uploads after ten minutes and clears progress', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest
    const onProgress = vi.fn()

    const promise = uploadChatAttachment(new File(['hello'], 'note.txt'), { onProgress })
    const xhr = FakeXMLHttpRequest.instances[0]

    xhr.upload.onprogress?.(
      new ProgressEvent('progress', {
        lengthComputable: true,
        loaded: 1,
        total: 2,
      }) as ProgressEvent<XMLHttpRequestEventTarget>,
    )
    const rejection = expect(promise).rejects.toThrow('Attachment upload timed out')
    xhr.ontimeout?.()

    await rejection
    expect(xhr.timeout).toBe(10 * 60 * 1000)
    expect(onProgress).toHaveBeenNthCalledWith(1, 0.5)
    expect(onProgress).toHaveBeenLastCalledWith(null)
  })

  it('rejects invalid upload JSON responses', async () => {
    globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest

    const promise = uploadChatAttachment(new File(['hello'], 'note.txt'))
    const xhr = FakeXMLHttpRequest.instances[0]
    xhr.status = 200
    xhr.responseText = '{bad'
    const rejection = expect(promise).rejects.toThrow('Attachment upload returned invalid JSON')
    xhr.onload?.()

    await rejection
  })
})

describe('deleteChatAttachment', () => {
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
    })
  })
})

describe('formatAttachmentSize', () => {
  it('clamps non-positive byte counts to zero bytes', () => {
    expect(formatAttachmentSize(0)).toBe('0 B')
    expect(formatAttachmentSize(-1)).toBe('0 B')
  })
})
