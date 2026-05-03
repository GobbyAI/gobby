import { useState, useCallback } from 'react'
import { cn } from '../../lib/utils'
import { SOURCE_BADGE_CLS, SOURCE_BADGE_BG, FORM_CANCEL_BTN_CLS, FORM_SAVE_BTN_CLS } from './styles'

const OVERLAY_CLS =
  'fixed inset-0 z-[100] flex items-center justify-center bg-[var(--surface-scrim)] [animation:fadeIn_0.15s_ease]'
const MODAL_CLS =
  'w-[500px] max-w-[90vw] overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-primary)]'
const HEADER_CLS = 'flex items-center justify-between border-b border-[var(--border)] px-5 py-4'
const HEADER_TITLE_CLS = 'm-0 text-[length:var(--text-lg)] font-semibold'
const CLOSE_CLS =
  'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-xl)] text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] pointer-coarse:h-11 pointer-coarse:w-11'
const BODY_CLS = 'flex flex-col gap-2 p-5'
const LABEL_CLS = 'text-[length:var(--text-sm)] font-medium text-[var(--text-muted)]'
const INPUT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-2 font-[inherit] text-[length:var(--text-base)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const ERROR_CLS = 'text-[length:var(--text-sm)] text-[var(--color-error)]'
const FOOTER_CLS = 'flex justify-end gap-2 border-t border-[var(--border)] px-5 py-3'

interface SkillImportModalProps {
  onImport: (source: string) => Promise<void>
  onClose: () => void
}

function detectSourceType(source: string): string {
  const s = source.trim()
  if (s.startsWith('github:') || s.startsWith('https://github.com') || s.startsWith('http://github.com')) return 'github'
  if (s.endsWith('.zip')) return 'zip'
  if (s.startsWith('/') || s.startsWith('./') || s.startsWith('~')) return 'local'
  if (s.includes('/') && !s.startsWith('http')) return 'github'
  return 'unknown'
}

export function SkillImportModal({ onImport, onClose }: SkillImportModalProps) {
  const [source, setSource] = useState('')
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const sourceType = source.trim() ? detectSourceType(source) : null

  const handleImport = useCallback(async () => {
    if (!source.trim()) return
    setImporting(true)
    setError(null)
    try {
      await onImport(source.trim())
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed')
    } finally {
      setImporting(false)
    }
  }, [source, onImport, onClose])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !importing) handleImport()
    if (e.key === 'Escape') onClose()
  }, [handleImport, importing, onClose])

  return (
    <div className={OVERLAY_CLS} onClick={onClose}>
      <div className={MODAL_CLS} onClick={e => e.stopPropagation()}>
        <div className={HEADER_CLS}>
          <h2 className={HEADER_TITLE_CLS}>Import Skill</h2>
          <button
            type="button"
            className={CLOSE_CLS}
            onClick={onClose}
            aria-label="Close import modal"
            title="Close"
          >
            &times;
          </button>
        </div>

        <div className={BODY_CLS}>
          <label className={LABEL_CLS}>Source URL or Path</label>
          <input
            className={INPUT_CLS}
            value={source}
            onChange={e => setSource(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="github:owner/repo, /path/to/skill, or file.zip"
            autoFocus
          />

          {sourceType && (
            <span className={cn(SOURCE_BADGE_CLS, SOURCE_BADGE_BG[sourceType] ?? SOURCE_BADGE_BG.unknown)}>
              {sourceType}
            </span>
          )}

          {error && <div className={ERROR_CLS}>{error}</div>}
        </div>

        <div className={FOOTER_CLS}>
          <button className={FORM_CANCEL_BTN_CLS} onClick={onClose}>Cancel</button>
          <button
            className={FORM_SAVE_BTN_CLS}
            onClick={handleImport}
            disabled={!source.trim() || importing}
          >
            {importing ? 'Importing...' : 'Import'}
          </button>
        </div>
      </div>
    </div>
  )
}
