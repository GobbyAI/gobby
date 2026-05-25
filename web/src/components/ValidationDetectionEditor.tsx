import { useMemo, useState } from 'react'
import {
  FIELD_HELP_CLS,
  FIELD_LABEL_CLS,
  FORM_FIELD_CLS,
  FORM_SECTION_CLS,
  INPUT_CLS,
  SECTION_BODY_CLS,
  SECTION_HEADER_STATIC_CLS,
  SECTION_TITLE_CLS,
  TOOLBAR_BTN_CLS,
} from './ConfigurationPage.styles'

const DEFAULT_DETECTION_CONFIG = {
  enabled: true,
  builtin_matchers_enabled: true,
  disabled_builtin_matcher_ids: [],
  recognized_wrappers: [],
  custom_matchers: [],
}

interface ValidationDetectionEditorProps {
  value: unknown
  onChange: (value: Record<string, unknown>) => void
  title?: string
}

interface PreviewResult {
  matched: boolean
  matcher_id?: string
  label?: string
  categories?: string[]
  languages?: string[]
}

function normalizeValue(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return DEFAULT_DETECTION_CONFIG
}

export function ValidationDetectionEditor({
  value,
  onChange,
  title = 'Validation Detection',
}: ValidationDetectionEditorProps) {
  const normalized = useMemo(() => normalizeValue(value), [value])
  const [jsonText, setJsonText] = useState(() => JSON.stringify(normalized, null, 2))
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [command, setCommand] = useState('')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const handleJsonChange = (next: string) => {
    setJsonText(next)
    try {
      const parsed = JSON.parse(next)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setJsonError('Expected a JSON object')
        return
      }
      setJsonError(null)
      onChange(parsed as Record<string, unknown>)
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : String(error))
    }
  }

  const previewCommand = async () => {
    setPreview(null)
    setPreviewError(null)
    try {
      const parsed = JSON.parse(jsonText) as Record<string, unknown>
      const response = await fetch('/api/config/validation-detection/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command, config: parsed }),
      })
      const data = await response.json()
      if (!response.ok) {
        setPreviewError(data.detail || 'Preview failed')
        return
      }
      setPreview(data)
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : String(error))
    }
  }

  return (
    <div className={FORM_SECTION_CLS}>
      <div className={SECTION_HEADER_STATIC_CLS}>
        <span className={SECTION_TITLE_CLS}>{title}</span>
      </div>
      <div className={SECTION_BODY_CLS}>
        <div className={FORM_FIELD_CLS}>
          <label className={FIELD_LABEL_CLS} htmlFor="validation-detection-json">
            Matcher Config
          </label>
          <span className={FIELD_HELP_CLS}>
            JSON object with enabled, builtin_matchers_enabled, disabled_builtin_matcher_ids,
            recognized_wrappers, and custom_matchers.
          </span>
          <textarea
            id="validation-detection-json"
            className={`${INPUT_CLS} min-h-[220px] resize-y leading-5`}
            value={jsonText}
            spellCheck={false}
            onChange={(event) => handleJsonChange(event.target.value)}
          />
          {jsonError && <span className="text-[length:var(--text-sm)] text-[var(--color-error)]">{jsonError}</span>}
        </div>

        <div className={FORM_FIELD_CLS}>
          <label className={FIELD_LABEL_CLS} htmlFor="validation-detection-preview">
            Preview Command
          </label>
          <div className="flex gap-2 max-sm:flex-col">
            <input
              id="validation-detection-preview"
              className={`${INPUT_CLS} flex-1`}
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder="cargo clippy --no-default-features -- -D warnings"
            />
            <button
              type="button"
              className={TOOLBAR_BTN_CLS}
              onClick={() => { void previewCommand() }}
              disabled={!command.trim() || Boolean(jsonError)}
            >
              Preview
            </button>
          </div>
          {preview && (
            <span className={FIELD_HELP_CLS}>
              {preview.matched
                ? `Matched ${preview.matcher_id}: ${preview.label}`
                : 'No validation matcher matched'}
            </span>
          )}
          {previewError && (
            <span className="text-[length:var(--text-sm)] text-[var(--color-error)]">
              {previewError}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
