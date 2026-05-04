import { useEffect, useState } from 'react'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import { useDaemonRestart } from '../hooks/useDaemonRestart'
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
  const { showRestart, restartError, markRestartRequired, restartDaemon } = useDaemonRestart()

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
      markRestartRequired()
    } else {
      setErrors(result.errors || ['Save failed'])
    }
  }

  return (
    <div className={YAML_CLS}>
      {showRestart && (
        <div className={RESTART_BANNER_CLS}>
          <span>Configuration saved to database. Restart the daemon to apply changes.</span>
          <button
            type="button"
            className={RESTART_BTN_CLS}
            onClick={() => { void restartDaemon() }}
          >
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
          {restartError && <span>{restartError}</span>}
        </div>
        <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Template'}
        </button>
      </div>
    </div>
  )
}
