import { useCallback, useState } from 'react'
import { requestDaemonRestart } from '../lib/api'

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
      const message = err instanceof Error ? err.message : 'Failed to restart daemon'
      setRestartError(message)
      return false
    }
  }, [])

  return { showRestart, restartError, markRestartRequired, restartDaemon }
}
