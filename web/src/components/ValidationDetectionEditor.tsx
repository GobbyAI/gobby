import { useMemo, useState } from 'react'

// Class tokens hoisted from the now-deleted legacy configuration-page styles
// module. This is the only surviving consumer (reused by the settings overlay's
// ProjectsSessionsSection), so the few classes it needs live here directly.
const FORM_SECTION_CLS =
  'mb-5 overflow-hidden rounded-lg border border-[var(--border)]'
const SECTION_HEADER_STATIC_CLS =
  'flex select-none items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5'
const SECTION_TITLE_CLS =
  'text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const SECTION_BODY_CLS = 'flex flex-col gap-3 px-3.5 py-3'
const FORM_FIELD_CLS = 'flex flex-col gap-1'
const FIELD_LABEL_CLS =
  'text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'
const FIELD_HELP_CLS =
  'text-[length:var(--text-xs)] leading-[1.4] text-[var(--text-muted)]'
const INPUT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 font-mono text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const TOOLBAR_BTN_CLS =
  'flex cursor-pointer items-center gap-1.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:border-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'

const DEFAULT_DETECTION_CONFIG = {
  enabled: true,
  builtin_matchers_enabled: true,
  disabled_builtin_matcher_ids: [],
  recognized_wrappers: [],
  wrapper_rules: [],
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
  const incoming = useMemo(() => JSON.stringify(normalized, null, 2), [normalized])
  const [jsonText, setJsonText] = useState(incoming)
  const [syncedValue, setSyncedValue] = useState(incoming)
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [command, setCommand] = useState('')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

  // Resync the editor text when the external value changes (e.g. a different
  // record is loaded), without clobbering in-progress edits that already
  // produced this same value. Adjusting state during render is the codebase
  // convention (see RulesDetailPanel).
  if (syncedValue !== incoming) {
    setSyncedValue(incoming)
    let currentCanonical: string | null = null
    try {
      currentCanonical = JSON.stringify(normalizeValue(JSON.parse(jsonText)), null, 2)
    } catch {
      currentCanonical = null
    }
    if (currentCanonical !== incoming) {
      setJsonText(incoming)
      setJsonError(null)
    }
  }

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
            recognized_wrappers, wrapper_rules, and custom_matchers.
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
