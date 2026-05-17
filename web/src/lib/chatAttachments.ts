import type { ChatAttachment } from '../types/chat'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

function attachmentUrl(path: string): string {
  if (!API_BASE_URL || !path.startsWith('/')) return path
  return `${API_BASE_URL}${path}`
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
    content_url: attachmentUrl(attachment.content_url),
  }
}

export function uploadChatAttachment(
  file: File,
  options: {
    draftId?: string
    onProgress?: (progress: number | null) => void
  } = {},
): Promise<ChatAttachment> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const form = new FormData()
    form.append('file', file)
    if (options.draftId) form.append('draft_id', options.draftId)

    xhr.open('POST', `${API_BASE_URL}/api/chat/attachments`)
    xhr.withCredentials = true
    xhr.upload.onprogress = (event) => {
      options.onProgress?.(event.lengthComputable ? event.loaded / event.total : null)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(normalizeAttachmentUrl(JSON.parse(xhr.responseText) as ChatAttachment))
        return
      }
      reject(new Error(errorFromResponse(xhr)))
    }
    xhr.onerror = () => reject(new Error('Attachment upload failed'))
    xhr.onabort = () => reject(new Error('Attachment upload canceled'))
    xhr.send(form)
  })
}

export function deleteChatAttachment(attachmentId: string): Promise<Response> {
  return fetch(`${API_BASE_URL}/api/chat/attachments/${attachmentId}`, {
    method: 'DELETE',
    credentials: 'include',
  })
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}
