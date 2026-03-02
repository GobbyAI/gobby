import { useState, useCallback, useRef } from 'react'
import { ConfirmDialog } from '../components/chat/ui/ConfirmDialog'

interface ConfirmOptions {
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
}

interface PendingConfirm extends ConfirmOptions {
  resolve: (value: boolean) => void
}

export function useConfirmDialog() {
  const [pending, setPending] = useState<PendingConfirm | null>(null)
  const pendingRef = useRef<PendingConfirm | null>(null)

  const confirm = useCallback((opts: ConfirmOptions): Promise<boolean> => {
    // If there's already a pending confirm, reject it
    if (pendingRef.current) {
      pendingRef.current.resolve(false)
    }
    return new Promise<boolean>((resolve) => {
      const p: PendingConfirm = { ...opts, resolve }
      pendingRef.current = p
      setPending(p)
    })
  }, [])

  const handleConfirm = useCallback(() => {
    pendingRef.current?.resolve(true)
    pendingRef.current = null
    setPending(null)
  }, [])

  const handleCancel = useCallback(() => {
    pendingRef.current?.resolve(false)
    pendingRef.current = null
    setPending(null)
  }, [])

  const ConfirmDialogElement = pending ? (
    <ConfirmDialog
      open
      title={pending.title}
      description={pending.description}
      confirmLabel={pending.confirmLabel}
      cancelLabel={pending.cancelLabel}
      destructive={pending.destructive}
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  ) : null

  return { confirm, ConfirmDialogElement }
}
