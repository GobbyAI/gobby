import { afterEach, describe, expect, it, vi } from 'vitest'

import { uploadChatAttachment } from '../chatAttachments'

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
})
