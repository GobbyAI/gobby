import { useState, useCallback, useRef } from 'react'
import type { GobbySkill } from '../../hooks/useSkills'
import { MemoizedMarkdown } from '../shared/MemoizedMarkdown'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import { FORM_CANCEL_BTN_CLS, FORM_SAVE_BTN_CLS } from './styles'
import { Heading } from '../shared/Heading'

const OVERLAY_CLS =
  'fixed inset-0 z-[100] flex items-center justify-center bg-[var(--surface-scrim)] [animation:fade-in_0.2s_ease]'
const MODAL_CLS =
  'flex max-h-[90vh] w-[90vw] max-w-[1000px] flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-primary)]'
const HEADER_CLS = 'flex items-center justify-between border-b border-[var(--border)] px-5 py-4'
const HEADER_TITLE_CLS = 'm-0 text-[length:var(--text-lg)] font-semibold'
const CLOSE_CLS =
  'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-xl)] text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] pointer-coarse:h-11 pointer-coarse:w-11'

const BODY_CLS = 'flex flex-1 flex-col overflow-y-auto'
const TOP_CLS = 'border-b border-[var(--border)] px-5 py-4'
const FIELDS_CLS = 'flex flex-col gap-2.5'
const ROW_CLS = 'flex flex-col gap-1'
const ROW_GROUP_CLS = 'flex gap-3'
const ROW_HALF_CLS = 'flex-1'

const LABEL_CLS = 'text-[length:var(--text-sm)] font-medium text-[var(--text-muted)]'
const INPUT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-base)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-60 pointer-coarse:min-h-11'
const SELECT_CLS = INPUT_CLS

const CHECKBOXES_CLS = 'flex gap-4'
const CHECKBOX_CLS =
  'flex cursor-pointer items-center gap-1.5 text-[length:var(--text-base)] text-[var(--text-secondary)]'

const EDITOR_CONTAINER_CLS = 'flex h-[40vh] border-b border-[var(--border)]'
const EDITOR_PANE_CLS = 'flex flex-1 flex-col overflow-hidden px-5 py-3'
const TEXTAREA_CLS =
  'flex-1 resize-none rounded border border-[var(--border)] bg-[var(--bg-secondary)] p-2 font-[inherit] text-[length:var(--text-base)] leading-[1.5] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'
const PREVIEW_PANE_CLS =
  'flex-1 overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-secondary)] px-5 py-3'
const PREVIEW_CONTENT_CLS = 'text-[length:var(--text-base)]'

const FOOTER_CLS = 'flex justify-end gap-2 border-t border-[var(--border)] px-5 py-3'

export interface SkillFormData {
  name: string
  description: string
  content: string
  version: string
  license: string
  compatibility: string
  allowed_tools: string[]
  enabled: boolean
  always_apply: boolean
  injection_format: string
}

interface SkillFormProps {
  skill: GobbySkill | null
  onSave: (data: SkillFormData) => void
  onCancel: () => void
}

export function SkillForm({ skill, onSave, onCancel }: SkillFormProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus({ ref: dialogRef, isOpen: true, onClose: onCancel })
  const [name, setName] = useState(skill?.name || '')
  const [description, setDescription] = useState(skill?.description || '')
  const [content, setContent] = useState(skill?.content || '')
  const [version, setVersion] = useState(skill?.version || '')
  const [license, setLicense] = useState(skill?.license || '')
  const [compatibility, setCompatibility] = useState(skill?.compatibility || '')
  const [allowedToolsStr, setAllowedToolsStr] = useState(skill?.allowed_tools?.join(', ') || '')
  const [enabled, setEnabled] = useState(skill?.enabled ?? true)
  const [alwaysApply, setAlwaysApply] = useState(skill?.always_apply ?? false)
  const [injectionFormat, setInjectionFormat] = useState(skill?.injection_format || 'summary')

  const handleSubmit = useCallback((e: React.FormEvent) => {
    e.preventDefault()
    const tools = allowedToolsStr
      .split(',')
      .map(t => t.trim())
      .filter(Boolean)

    onSave({
      name,
      description,
      content,
      version,
      license,
      compatibility,
      allowed_tools: tools.length > 0 ? tools : [],
      enabled,
      always_apply: alwaysApply,
      injection_format: injectionFormat,
    })
  }, [name, description, content, version, license, compatibility, allowedToolsStr, enabled, alwaysApply, injectionFormat, onSave])

  return (
    <div className={OVERLAY_CLS} onClick={onCancel}>
      <div ref={dialogRef} className={MODAL_CLS} role="dialog" aria-modal="true" aria-labelledby="skill-form-title" tabIndex={-1} onClick={e => e.stopPropagation()}>
        <div className={HEADER_CLS}>
          <Heading level={2} id="skill-form-title" className={HEADER_TITLE_CLS}>{skill ? 'Edit Skill' : 'New Skill'}</Heading>
          <button
            type="button"
            className={CLOSE_CLS}
            onClick={onCancel}
            aria-label="Close skill form"
            title="Close"
          >
            &times;
          </button>
        </div>

        <form onSubmit={handleSubmit} className={BODY_CLS}>
          <div className={TOP_CLS}>
            <div className={FIELDS_CLS}>
              <label className={ROW_CLS}>
                <span className={LABEL_CLS}>Name</span>
                <input
                  className={INPUT_CLS}
                  value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder="my-skill"
                  required
                  disabled={!!skill}
                />
              </label>

              <label className={ROW_CLS}>
                <span className={LABEL_CLS}>Description</span>
                <input
                  className={INPUT_CLS}
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  placeholder="What this skill does"
                  required
                />
              </label>

              <div className={ROW_GROUP_CLS}>
                <label className={`${ROW_CLS} ${ROW_HALF_CLS}`}>
                  <span className={LABEL_CLS}>Version</span>
                  <input
                    className={INPUT_CLS}
                    value={version}
                    onChange={e => setVersion(e.target.value)}
                    placeholder="1.0.0"
                  />
                </label>
                <label className={`${ROW_CLS} ${ROW_HALF_CLS}`}>
                  <span className={LABEL_CLS}>License</span>
                  <input
                    className={INPUT_CLS}
                    value={license}
                    onChange={e => setLicense(e.target.value)}
                    placeholder="MIT"
                  />
                </label>
              </div>

              <label className={ROW_CLS}>
                <span className={LABEL_CLS}>Compatibility</span>
                <input
                  className={INPUT_CLS}
                  value={compatibility}
                  onChange={e => setCompatibility(e.target.value)}
                  placeholder="Claude Code, Gemini CLI"
                />
              </label>

              <label className={ROW_CLS}>
                <span className={LABEL_CLS}>Allowed Tools (comma-separated)</span>
                <input
                  className={INPUT_CLS}
                  value={allowedToolsStr}
                  onChange={e => setAllowedToolsStr(e.target.value)}
                  placeholder="Edit, Write, Bash"
                />
              </label>

              <label className={ROW_CLS}>
                <span className={LABEL_CLS}>Injection Format</span>
                <select
                  className={SELECT_CLS}
                  value={injectionFormat}
                  onChange={e => setInjectionFormat(e.target.value)}
                >
                  <option value="summary">Summary</option>
                  <option value="full">Full</option>
                  <option value="content">Content Only</option>
                </select>
              </label>

              <div className={CHECKBOXES_CLS}>
                <label className={CHECKBOX_CLS}>
                  <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
                  Enabled
                </label>
                <label className={CHECKBOX_CLS}>
                  <input type="checkbox" checked={alwaysApply} onChange={e => setAlwaysApply(e.target.checked)} />
                  Always Apply
                </label>
              </div>
            </div>
          </div>

          <div className={EDITOR_CONTAINER_CLS}>
            <label className={EDITOR_PANE_CLS}>
              <span className={LABEL_CLS}>Content (Markdown)</span>
              <textarea
                className={TEXTAREA_CLS}
                value={content}
                onChange={e => setContent(e.target.value)}
                placeholder="# Skill Instructions&#10;&#10;Write your skill content here..."
                spellCheck={false}
              />
            </label>
            <div className={PREVIEW_PANE_CLS}>
              <span className={LABEL_CLS}>Preview</span>
              <div className={PREVIEW_CONTENT_CLS}>
                <MemoizedMarkdown content={content || '*No content yet*'} id="skill-form-preview" />
              </div>
            </div>
          </div>

          <div className={FOOTER_CLS}>
            <button type="button" className={FORM_CANCEL_BTN_CLS} onClick={onCancel}>Cancel</button>
            <button type="submit" className={FORM_SAVE_BTN_CLS}>
              {skill ? 'Save Changes' : 'Create Skill'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
