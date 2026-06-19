import { useCallback, useEffect, useRef, useState } from 'react'
import type { QueuedFile } from '../../types/chat'
import { deleteChatAttachment, uploadChatAttachment } from '../../lib/chatAttachments'

interface UseChatInputAttachmentsOptions {
  attachmentsDisabled: boolean
  projectId?: string | null
}

export function useChatInputAttachments({
  attachmentsDisabled,
  projectId,
}: UseChatInputAttachmentsOptions) {
  const [queuedFiles, setQueuedFiles] = useState<QueuedFile[]>([])
  const attachmentsDisabledRef = useRef(attachmentsDisabled)
  const mountedRef = useRef(true)
  const queuedFilesRef = useRef(queuedFiles)
  const deletedUploadedAttachmentIdsRef = useRef<Set<string>>(new Set())
  const deleteUploadedAttachmentRef = useRef<(attachmentId: string) => void>(() => {})

  useEffect(() => {
    queuedFilesRef.current = queuedFiles
  }, [queuedFiles])

  useEffect(() => {
    attachmentsDisabledRef.current = attachmentsDisabled
  }, [attachmentsDisabled])

  useEffect(() => {
    return () => {
      mountedRef.current = false
    }
  }, [])

  const deleteUploadedAttachment = useCallback((attachmentId: string) => {
    if (deletedUploadedAttachmentIdsRef.current.has(attachmentId)) return
    deletedUploadedAttachmentIdsRef.current.add(attachmentId)
    void deleteChatAttachment(attachmentId).catch((error: unknown) => {
      console.warn('Failed to delete uploaded chat attachment', { attachmentId, error })
    })
  }, [])

  useEffect(() => {
    deleteUploadedAttachmentRef.current = deleteUploadedAttachment
  }, [deleteUploadedAttachment])

  const clearQueuedFiles = useCallback(
    (deleteUploaded = false) => {
      queuedFilesRef.current.forEach((queuedFile) => {
        if (queuedFile.previewUrl) URL.revokeObjectURL(queuedFile.previewUrl)
        queuedFile.uploadAbort?.()
        if (deleteUploaded && queuedFile.attachment) {
          deleteUploadedAttachment(queuedFile.attachment.id)
        }
      })
      queuedFilesRef.current = []
      setQueuedFiles([])
      deletedUploadedAttachmentIdsRef.current.clear()
    },
    [deleteUploadedAttachment],
  )

  useEffect(() => {
    const deletedUploadedAttachmentIds = deletedUploadedAttachmentIdsRef.current
    return () => {
      queuedFilesRef.current.forEach((queuedFile) => {
        if (queuedFile.previewUrl) URL.revokeObjectURL(queuedFile.previewUrl)
        queuedFile.uploadAbort?.()
        if (queuedFile.attachment) deleteUploadedAttachmentRef.current(queuedFile.attachment.id)
      })
      deletedUploadedAttachmentIds.clear()
    }
  }, [])

  useEffect(() => {
    if (queuedFiles.length === 0) {
      deletedUploadedAttachmentIdsRef.current.clear()
    }
  }, [queuedFiles.length])

  useEffect(() => {
    if (attachmentsDisabled) {
      clearQueuedFiles(true)
    }
  }, [attachmentsDisabled, clearQueuedFiles])

  const uploadQueuedFile = useCallback(
    async (id: string, file: File) => {
      const disabledAtStart = attachmentsDisabledRef.current
      const upload = uploadChatAttachment(file, {
        projectId,
        onProgress: (progress) => {
          if (!mountedRef.current) return
          setQueuedFiles((prev) =>
            prev.map((queuedFile) =>
              queuedFile.id === id ? { ...queuedFile, progress } : queuedFile,
            ),
          )
        },
      })
      setQueuedFiles((prev) =>
        prev.map((queuedFile) =>
          queuedFile.id === id ? { ...queuedFile, uploadAbort: upload.abort } : queuedFile,
        ),
      )
      try {
        const attachment = await upload.promise
        if (!mountedRef.current) {
          deleteUploadedAttachment(attachment.id)
          return
        }
        setQueuedFiles((prev) => {
          const stillQueued = prev.some((queuedFile) => queuedFile.id === id)
          if (!stillQueued || disabledAtStart || attachmentsDisabledRef.current) {
            deleteUploadedAttachment(attachment.id)
            return prev
          }
          return prev.map((queuedFile) =>
            queuedFile.id === id
              ? {
                  ...queuedFile,
                  status: 'uploaded',
                  progress: 1,
                  attachment,
                  error: null,
                  uploadAbort: null,
                }
              : queuedFile,
          )
        })
      } catch (error: unknown) {
        if (!mountedRef.current) return
        const message = error instanceof Error ? error.message : 'Attachment upload failed'
        setQueuedFiles((prev) => {
          const stillQueued = prev.some((queuedFile) => queuedFile.id === id)
          if (message === 'Attachment upload canceled' && !stillQueued) return prev
          console.warn('Attachment upload failed', { id, error })
          return prev.map((queuedFile) =>
            queuedFile.id === id
              ? {
                  ...queuedFile,
                  status: 'error',
                  progress: null,
                  attachment: null,
                  error: message,
                  uploadAbort: null,
                }
              : queuedFile,
          )
        })
      }
    },
    [deleteUploadedAttachment, projectId],
  )

  const handleFilesSelected = useCallback(
    (files: FileList | null) => {
      if (!files || attachmentsDisabled) return
      Array.from(files).forEach((file) => {
        const id = crypto.randomUUID()
        const isImage = file.type.startsWith('image/')
        const previewUrl = isImage ? URL.createObjectURL(file) : null
        setQueuedFiles((prev) => [
          ...prev,
          {
            id,
            file,
            previewUrl,
            status: 'uploading',
            progress: null,
            attachment: null,
            error: null,
            uploadAbort: null,
          },
        ])
        void uploadQueuedFile(id, file)
      })
    },
    [attachmentsDisabled, uploadQueuedFile],
  )

  const removeFile = useCallback(
    (id: string) => {
      setQueuedFiles((prev) => {
        const removed = prev.find((file) => file.id === id)
        if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl)
        removed?.uploadAbort?.()
        if (removed?.attachment) deleteUploadedAttachment(removed.attachment.id)
        return prev.filter((file) => file.id !== id)
      })
    },
    [deleteUploadedAttachment],
  )

  const retryFile = useCallback(
    (id: string) => {
      const queued = queuedFilesRef.current.find((queuedFile) => queuedFile.id === id)
      if (!queued) return
      setQueuedFiles((prev) =>
        prev.map((queuedFile) =>
          queuedFile.id === id
            ? {
                ...queuedFile,
                status: 'uploading',
                progress: null,
                attachment: null,
                error: null,
                uploadAbort: null,
              }
            : queuedFile,
        ),
      )
      void uploadQueuedFile(id, queued.file)
    },
    [uploadQueuedFile],
  )

  return {
    clearQueuedFiles,
    handleFilesSelected,
    hasPendingUploads: queuedFiles.some((queuedFile) => queuedFile.status === 'uploading'),
    hasUploadErrors: queuedFiles.some((queuedFile) => queuedFile.status === 'error'),
    queuedFiles,
    removeFile,
    retryFile,
  }
}
