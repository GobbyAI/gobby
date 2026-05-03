import { useEffect, useState } from 'react'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import { cn } from '../lib/utils'
import {
  RESTART_BANNER_CLS,
  RESTART_BTN_CLS,
  TOOLBAR_BTN_CLS,
  TOOLBAR_BTN_PRIMARY_CLS,
  YAML_CLS,
  YAML_EDITOR_CLS,
  YAML_ERRORS_CLS,
  YAML_FOOTER_CLS,
} from './ConfigurationPage.styles'

interface TemplateTabProps {
  content: string
  onFetch: () => Promise<void>
  onSave: (content: string) => Promise<{ ok: boolean; errors?: string[] }>
}

export function TemplateTab({ content, onFetch, onSave }: TemplateTabProps) {
  const [localContent, setLocalContent] = useState(content)
  const [errors, setErrors] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [showRestart, setShowRestart] = useState(false)

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLocalContent(content)
  }, [content])

  useEffect(() => {
    onFetch()
  }, [onFetch])

  const handleSave = async () => {
    setSaving(true)
    setErrors([])
    const result = await onSave(localContent)
    setSaving(false)
    if (result.ok) {
      setShowRestart(true)
    } else {
      setErrors(result.errors || ['Save failed'])
    }
  }

  const handleRestart = async () => {
    setErrors([])
    try {
      const res = await fetch(`${import.meta.env.VITE_API_BASE_URL || ''}/api/admin/restart`, {
        method: 'POST',
      })
      if (!res.ok) {
        throw new Error(`Restart failed: ${res.status}`)
      }
      setShowRestart(false)
    } catch (err) {
      console.error('Failed to restart daemon:', err)
      setErrors(['Failed to restart daemon'])
    }
  }

  return (
    <div className={YAML_CLS}>
      {showRestart && (
        <div className={RESTART_BANNER_CLS}>
          <span>Configuration saved to database. Restart the daemon to apply changes.</span>
          <button type="button" className={RESTART_BTN_CLS} onClick={handleRestart}>
            Restart Now
          </button>
        </div>
      )}
      <div className={YAML_EDITOR_CLS}>
        <CodeMirrorEditor
          content={localContent}
          language="yaml"
          onChange={setLocalContent}
          onSave={handleSave}
        />
      </div>
      <div className={YAML_FOOTER_CLS}>
        <div className={YAML_ERRORS_CLS}>
          {errors.map((e, i) => <span key={i}>{e}</span>)}
        </div>
        <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Template'}
        </button>
      </div>
    </div>
  )
}
