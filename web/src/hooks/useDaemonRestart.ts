import { useCallback, useState } from 'react'
import { requestDaemonRestart } from '../lib/api'

function getRestartErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  return 'Failed to restart daemon'
}

export function useDaemonRestart() {
  const [showRestart, setShowRestart] = useState(false)
  const [restartError, setRestartError] = useState<string | null>(null)

  const markRestartRequired = useCallback(() => {
    setRestartError(null)
    setShowRestart(true)
  }, [])

  const restartDaemon = useCallback(async () => {
    setRestartError(null)
    try {
      const res = await requestDaemonRestart()
      if (!res.ok) throw new Error(`Restart failed: ${res.status}`)
      setShowRestart(false)
      return true
    } catch (err) {
      console.error('Failed to restart daemon:', err)
      setRestartError(getRestartErrorMessage(err))
      return false
    }
  }, [])

  return { showRestart, restartError, markRestartRequired, restartDaemon }
}
