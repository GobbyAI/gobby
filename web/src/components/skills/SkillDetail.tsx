import { useState, useCallback } from 'react'
import type { GobbySkill, ScanResult } from '../../hooks/useSkills'
import { MemoizedMarkdown } from '../shared/MemoizedMarkdown'
import { SkillScanPanel } from './SkillScanPanel'
import { cn } from '../../lib/utils'
import { SOURCE_BADGE_CLS, SOURCE_BADGE_BG } from './styles'

const DETAIL_CLS =
  'fixed bottom-0 right-0 top-12 z-50 flex w-[480px] max-w-[90vw] flex-col overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-primary)] [box-shadow:-4px_0_12px_oklch(0%_0_0_/_0.1)] max-md:w-screen max-md:max-w-none max-md:border-l-0 max-md:shadow-none'
const HEADER_CLS = 'flex items-center justify-between border-b border-[var(--border)] px-5 py-4'
const TITLE_CLS = 'm-0 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const CLOSE_CLS =
  'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-xl)] text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'

const META_CLS = 'flex flex-col gap-1.5 border-b border-[var(--border)] px-5 py-3'
const META_ROW_CLS = 'flex items-center gap-2 text-[length:var(--text-sm)]'
const LABEL_CLS = 'min-w-[90px] text-[var(--text-muted)]'
const STATUS_ENABLED_CLS = 'font-medium text-[var(--color-success-foreground)]'
const STATUS_DISABLED_CLS = 'font-medium text-[var(--color-error)]'

const DESCRIPTION_CLS =
  'border-b border-[var(--border)] px-5 py-3 text-[length:var(--text-base)] text-[var(--text-secondary)]'
const ACTIONS_CLS = 'flex gap-1.5 border-b border-[var(--border)] px-5 py-3'
const ACTION_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-all duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const ACTION_BTN_SCAN_CLS =
  'border-[color-mix(in_srgb,var(--color-agent)_30%,transparent)] text-[var(--accent)]'

const SCAN_ERROR_CLS = 'px-5 py-2 text-[length:var(--text-sm)] text-[var(--color-error)]'
const CONTENT_CLS = 'flex-1 px-5 py-4 text-[length:var(--text-base)]'

interface SkillDetailProps {
  skill: GobbySkill | null
  onClose: () => void
  onEdit: (skill: GobbySkill) => void
  onExport: (skillId: string) => void
  onScan: (content: string, name: string) => Promise<ScanResult | null>
}

export function SkillDetail({ skill, onClose, onEdit, onExport, onScan }: SkillDetailProps) {
  const [scanResult, setScanResult] = useState<ScanResult | null>(null)
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState<string | null>(null)

  const handleScan = useCallback(async () => {
    if (!skill) return
    setScanning(true)
    setScanError(null)
    try {
      const result = await onScan(skill.content, skill.name)
      setScanResult(result)
    } catch (e) {
      setScanError(e instanceof Error ? e.message : 'Scan failed')
    } finally {
      setScanning(false)
    }
  }, [skill, onScan])

  if (!skill) return null

  const category = skill.metadata?.category
    || (skill.metadata?.skillport as Record<string, unknown>)?.category
    || null

  return (
    <div className={DETAIL_CLS}>
      <div className={HEADER_CLS}>
        <h3 className={TITLE_CLS}>{skill.name}</h3>
        <button
          type="button"
          className={CLOSE_CLS}
          onClick={onClose}
          title="Close"
          aria-label="Close skill details"
        >
          &times;
        </button>
      </div>

      <div className={META_CLS}>
        <div className={META_ROW_CLS}>
          <span className={LABEL_CLS}>Status</span>
          <span className={skill.enabled ? STATUS_ENABLED_CLS : STATUS_DISABLED_CLS}>
            {skill.enabled ? 'Enabled' : 'Disabled'}
          </span>
        </div>
        {skill.version && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Version</span>
            <span>{skill.version}</span>
          </div>
        )}
        {skill.source_type && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Source</span>
            <span className={cn(SOURCE_BADGE_CLS, SOURCE_BADGE_BG[skill.source_type] ?? SOURCE_BADGE_BG.unknown)}>
              {skill.source_type}
            </span>
          </div>
        )}
        {category && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Category</span>
            <span>{String(category)}</span>
          </div>
        )}
        {skill.injection_format && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Format</span>
            <span>{skill.injection_format}</span>
          </div>
        )}
        {skill.always_apply && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Always Apply</span>
            <span>Yes</span>
          </div>
        )}
        {skill.hub_name && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Hub</span>
            <span>{skill.hub_name}{skill.hub_slug ? ` / ${skill.hub_slug}` : ''}</span>
          </div>
        )}
        {skill.allowed_tools && skill.allowed_tools.length > 0 && (
          <div className={META_ROW_CLS}>
            <span className={LABEL_CLS}>Allowed Tools</span>
            <span>{skill.allowed_tools.join(', ')}</span>
          </div>
        )}
        <div className={META_ROW_CLS}>
          <span className={LABEL_CLS}>Updated</span>
          <span>{new Date(skill.updated_at).toLocaleString()}</span>
        </div>
      </div>

      <div className={DESCRIPTION_CLS}>{skill.description}</div>

      <div className={ACTIONS_CLS}>
        <button className={ACTION_BTN_CLS} onClick={() => onEdit(skill)}>Edit</button>
        <button className={ACTION_BTN_CLS} onClick={() => onExport(skill.id)}>Export</button>
        <button
          className={cn(ACTION_BTN_CLS, ACTION_BTN_SCAN_CLS)}
          onClick={handleScan}
          disabled={scanning}
        >
          {scanning ? 'Scanning...' : 'Safety Scan'}
        </button>
      </div>

      {scanError && <div className={SCAN_ERROR_CLS}>{scanError}</div>}
      {scanResult && <SkillScanPanel result={scanResult} />}

      <div className={CONTENT_CLS}>
        <MemoizedMarkdown content={skill.content} id={`skill-detail-${skill.id}`} />
      </div>
    </div>
  )
}
