import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ChatInput } from '../ChatInput'

class MockXMLHttpRequest {
  static instances: MockXMLHttpRequest[] = []

  upload: { onprogress?: (event: ProgressEvent) => void } = {}
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  ontimeout: (() => void) | null = null
  status = 0
  statusText = ''
  responseText = ''
  requestBody: XMLHttpRequestBodyInit | null = null
  withCredentials = false
  timeout = 0

  constructor() {
    MockXMLHttpRequest.instances.push(this)
  }

  open = vi.fn()

  send(body?: XMLHttpRequestBodyInit | null) {
    this.requestBody = body ?? null
  }

  abort() {
    this.onabort?.()
  }

  respond(body: unknown, status = 200) {
    this.status = status
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

describe('ChatInput attachments', () => {
  const originalXHR = globalThis.XMLHttpRequest
  const originalFileReader = globalThis.FileReader
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    MockXMLHttpRequest.instances = []
    vi.stubGlobal('XMLHttpRequest', MockXMLHttpRequest)
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(
      '00000000-0000-4000-8000-000000000001',
    )
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 200 })))
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.stubGlobal('XMLHttpRequest', originalXHR)
    vi.stubGlobal('FileReader', originalFileReader)
    vi.stubGlobal('fetch', originalFetch)
  })

  it('uploads selected files with multipart form data without FileReader base64', async () => {
    const readAsDataURL = vi.fn()
    class MockFileReader {
      result: string | ArrayBuffer | null = null
      error: DOMException | null = null
      readyState = 0
      onabort: ((this: FileReader, ev: ProgressEvent<FileReader>) => unknown) | null = null
      onerror: ((this: FileReader, ev: ProgressEvent<FileReader>) => unknown) | null = null
      onload: ((this: FileReader, ev: ProgressEvent<FileReader>) => unknown) | null = null
      onloadend: ((this: FileReader, ev: ProgressEvent<FileReader>) => unknown) | null = null
      onloadstart: ((this: FileReader, ev: ProgressEvent<FileReader>) => unknown) | null = null
      onprogress: ((this: FileReader, ev: ProgressEvent<FileReader>) => unknown) | null = null
      abort = vi.fn()
      readAsArrayBuffer = vi.fn()
      readAsBinaryString = vi.fn()
      readAsText = vi.fn()
      readAsDataURL = readAsDataURL
      addEventListener = vi.fn()
      removeEventListener = vi.fn()
      dispatchEvent = vi.fn(() => true)
      EMPTY = 0 as const
      LOADING = 1 as const
      DONE = 2 as const
    }
    vi.stubGlobal('FileReader', MockFileReader)
    const onSend = vi.fn()
    const { container } = render(<ChatInput onSend={onSend} projectId="proj-1" />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['hello'], 'note.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })

    const xhr = MockXMLHttpRequest.instances[0]
    expect(xhr.requestBody).toBeInstanceOf(FormData)
    expect((xhr.requestBody as FormData).get('project_id')).toBe('proj-1')
    expect(readAsDataURL).not.toHaveBeenCalled()

    xhr.respond({
      id: 'att-1',
      project_id: 'proj-1',
      filename: 'note.txt',
      mime_type: 'text/plain',
      size_bytes: 5,
      content_url: '/api/chat/attachments/att-1/content',
    })

    await waitFor(() => expect(screen.getByTitle('Send message')).not.toBeDisabled())
    fireEvent.click(screen.getByTitle('Send message'))

    expect(onSend).toHaveBeenCalledWith(
      '',
      [
        expect.objectContaining({
          attachment: expect.objectContaining({ id: 'att-1' }),
          status: 'uploaded',
        }),
      ],
      expect.any(Object),
    )
  })

  it('keeps submit disabled until upload finishes', async () => {
    const onSend = vi.fn()
    const { container } = render(<ChatInput onSend={onSend} projectId="proj-1" />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['hello'], 'note.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })
    const sendButton = screen.getByTitle('Send message')

    expect(sendButton).toBeDisabled()
    fireEvent.click(sendButton)
    expect(onSend).not.toHaveBeenCalled()

    MockXMLHttpRequest.instances[0].respond({
      id: 'att-1',
      project_id: 'proj-1',
      filename: 'note.txt',
      mime_type: 'text/plain',
      size_bytes: 5,
      content_url: '/api/chat/attachments/att-1/content',
    })

    await waitFor(() => expect(sendButton).not.toBeDisabled())
  })

  it('deletes uploaded unsent attachments when unmounted', async () => {
    const onSend = vi.fn()
    const { container, unmount } = render(<ChatInput onSend={onSend} projectId="proj-1" />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['hello'], 'note.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })
    MockXMLHttpRequest.instances[0].respond({
      id: 'att-1',
      project_id: 'proj-1',
      filename: 'note.txt',
      mime_type: 'text/plain',
      size_bytes: 5,
      content_url: '/api/chat/attachments/att-1/content',
    })

    await waitFor(() => expect(screen.getByTitle('Send message')).not.toBeDisabled())
    unmount()

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/chat/attachments/att-1', {
        method: 'DELETE',
        credentials: 'include',
        signal: expect.any(AbortSignal),
      })
    })
  })

  it('does not delete uploaded attachments after sending', async () => {
    const onSend = vi.fn()
    const { container, unmount } = render(<ChatInput onSend={onSend} projectId="proj-1" />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['hello'], 'note.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })
    MockXMLHttpRequest.instances[0].respond({
      id: 'att-1',
      project_id: 'proj-1',
      filename: 'note.txt',
      mime_type: 'text/plain',
      size_bytes: 5,
      content_url: '/api/chat/attachments/att-1/content',
    })

    await waitFor(() => expect(screen.getByTitle('Send message')).not.toBeDisabled())
    fireEvent.click(screen.getByTitle('Send message'))
    expect(onSend).toHaveBeenCalled()
    unmount()

    expect(fetch).not.toHaveBeenCalled()
  })

  it('aborts in-flight uploads when attachments become disabled', async () => {
    const onSend = vi.fn()
    const { container, rerender } = render(
      <ChatInput onSend={onSend} projectId="proj-1" attachmentsDisabled={false} />,
    )
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['hello'], 'note.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })
    const xhr = MockXMLHttpRequest.instances[0]
    const abortSpy = vi.spyOn(xhr, 'abort')

    rerender(<ChatInput onSend={onSend} projectId="proj-1" attachmentsDisabled />)

    await waitFor(() => expect(abortSpy).toHaveBeenCalled())
  })
})
