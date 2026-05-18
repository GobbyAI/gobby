import type { ChatAttachment } from '../types/chat'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''
const ATTACHMENT_UPLOAD_TIMEOUT_MS = 10 * 60 * 1000

export interface ChatAttachmentUpload {
  promise: Promise<ChatAttachment>
  abort: () => void
}

function apiUrl(path: string): string {
  if (!API_BASE_URL || !path.startsWith('/')) return path
  return `${API_BASE_URL}${path}`
}

function isChatAttachment(value: unknown): value is ChatAttachment {
  if (!value || typeof value !== 'object') return false
  const record = value as Record<string, unknown>
  return (
    typeof record.id === 'string' &&
    typeof record.project_id === 'string' &&
    typeof record.filename === 'string' &&
    typeof record.mime_type === 'string' &&
    typeof record.size_bytes === 'number' &&
    typeof record.content_url === 'string'
  )
}

function parseChatAttachment(value: unknown): ChatAttachment {
  if (!isChatAttachment(value)) {
    throw new Error('Attachment upload returned invalid payload')
  }
  return value
}

function errorFromResponse(xhr: XMLHttpRequest): string {
  try {
    const parsed = JSON.parse(xhr.responseText) as { detail?: unknown }
    if (typeof parsed.detail === 'string') return parsed.detail
  } catch {
    // Fall through to status text.
  }
  return xhr.statusText || 'Attachment upload failed'
}

export function normalizeAttachmentUrl(attachment: ChatAttachment): ChatAttachment {
  return {
    ...attachment,
    content_url: apiUrl(attachment.content_url),
  }
}

export function uploadChatAttachment(
  file: File,
  options: {
    draftId?: string
    projectId?: string | null
    onProgress?: (progress: number | null) => void
  } = {},
): ChatAttachmentUpload {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<ChatAttachment>((resolve, reject) => {
    const form = new FormData()
    form.append('file', file)
    if (options.draftId) form.append('draft_id', options.draftId)
    if (options.projectId) form.append('project_id', options.projectId)

    xhr.open('POST', apiUrl('/api/chat/attachments'))
    xhr.withCredentials = true
    xhr.timeout = ATTACHMENT_UPLOAD_TIMEOUT_MS
    xhr.upload.onprogress = (event) => {
      options.onProgress?.(event.lengthComputable ? event.loaded / event.total : null)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(normalizeAttachmentUrl(parseChatAttachment(JSON.parse(xhr.responseText))))
        } catch (error) {
          reject(
            error instanceof SyntaxError
              ? new Error('Attachment upload returned invalid JSON')
              : error,
          )
        }
        return
      }
      reject(new Error(errorFromResponse(xhr)))
    }
    xhr.onerror = () => reject(new Error('Attachment upload failed'))
    xhr.onabort = () => {
      options.onProgress?.(null)
      reject(new Error('Attachment upload canceled'))
    }
    xhr.ontimeout = () => {
      options.onProgress?.(null)
      reject(new Error('Attachment upload timed out'))
    }
    xhr.send(form)
  })
  return {
    promise,
    abort: () => xhr.abort(),
  }
}

export async function deleteChatAttachment(attachmentId: string): Promise<Response> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), ATTACHMENT_UPLOAD_TIMEOUT_MS)
  try {
    const response = await fetch(apiUrl(`/api/chat/attachments/${encodeURIComponent(attachmentId)}`), {
      method: 'DELETE',
      credentials: 'include',
      signal: controller.signal,
    })
    if (!response.ok) {
      const body = await response.text().catch(() => '')
      throw new Error(body || response.statusText || `Attachment delete failed (${response.status})`)
    }
    return response
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('Attachment delete timed out')
    }
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes <= 0) return '0 B'
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}
