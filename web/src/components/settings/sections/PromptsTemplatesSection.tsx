import { useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import { CodeMirrorEditor } from '../../shared/CodeMirrorEditor'
import { DetailActionButton, SelectField } from '../../activity/fields'
import type { FieldOption } from '../../activity/fields'
import { Subsection } from './configFields'
import { SettingsSection } from './SettingsSection'
import { useSettingsSectionContext } from './SettingsSectionContext'
import type { SettingsSectionId } from '../sections'
import { useDaemonRestart } from '../../../hooks/useDaemonRestart'
import type {
  ConfigExportBundle,
  PromptDetail,
  PromptInfo,
} from '../../../hooks/useConfiguration'

/**
 * Prompts & Templates settings section (audit IA section 7, rows 78-82). Three
 * standalone surfaces live here, none of them DaemonConfig draft rows, so the
 * section owns no paths and renders no Save/Discard footer:
 *
 * - Prompt overrides: per-prompt markdown override editor (`/api/config/prompts`).
 * - Full configuration template: the whole defaults-plus-overrides YAML document
 *   (`/api/config/template`), which requires a daemon restart to apply.
 * - Backup & restore: export/import of the config bundle (`/api/config/export`,
 *   `/api/config/import`).
 *
 * Each surface saves immediately through its own controls (like the secret store
 * and rules-enforcement toggle), threaded through SettingsSectionContext. The two
 * CodeMirror editors register their unsaved-edit state with the section dirty
 * guard so switching sections or closing the overlay prompts to discard.
 */

const SECTION_ID: SettingsSectionId = 'prompts-templates'
const OWNED_PATHS: readonly string[] = []

type ImportBundle = {
  config_store?: Record<string, unknown>
  config?: Record<string, unknown>
  prompts?: Record<string, string>
}

type SurfaceStatus = { ok: boolean; message: string }

/**
 * Register an additional dirty guard for this section keyed to a standalone
 * editor's unsaved state. The overlay stores guards per section as a set, so the
 * always-false config-draft guard from the section shell and these editor guards
 * coexist; the section counts as dirty if any reports dirty.
 */
function useSectionDirtyGuard(dirty: boolean): void {
  const { registerDirtyGuard } = useSettingsSectionContext()
  const dirtyRef = useRef(dirty)
  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])
  useEffect(
    () => registerDirtyGuard(SECTION_ID, () => dirtyRef.current),
    [registerDirtyGuard],
  )
}

// `All prompts` first, then server-known categories with counts, sorted stable.
function categoryFilterOptions(
  total: number,
  categories: Record<string, number>,
): FieldOption[] {
  const options: FieldOption[] = [
    { value: '', label: `All prompts (${total})` },
  ]
  for (const [name, count] of Object.entries(categories).sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    options.push({ value: name, label: `${name} (${count})` })
  }
  return options
}

interface PromptOverridesGroupProps {
  prompts: PromptInfo[]
  categories: Record<string, number>
  onGetDetail?: (path: string) => Promise<PromptDetail | null>
  onSaveOverride?: (path: string, content: string) => Promise<boolean>
  onDeleteOverride?: (path: string) => Promise<boolean>
}

function PromptOverridesGroup({
  prompts,
  categories,
  onGetDetail,
  onSaveOverride,
  onDeleteOverride,
}: PromptOverridesGroupProps) {
  const [category, setCategory] = useState('')
  const [detail, setDetail] = useState<PromptDetail | null>(null)
  const [editContent, setEditContent] = useState('')
  const [savedContent, setSavedContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const dirty = detail !== null && editContent !== savedContent
  useSectionDirtyGuard(dirty)

  const options = useMemo(
    () => categoryFilterOptions(prompts.length, categories),
    [prompts.length, categories],
  )
  const filtered = useMemo(
    () => (category ? prompts.filter((p) => p.category === category) : prompts),
    [prompts, category],
  )

  // The override editor edits persisted content in place; without its write
  // handlers there is nothing actionable, so degrade to a notice.
  if (!onGetDetail || !onSaveOverride || !onDeleteOverride) {
    return (
      <Subsection
        title="Prompt overrides"
        hint="Customize the bundled prompt bodies the daemon ships with."
      >
        <p className="settings-section__pending">Prompt editing is unavailable.</p>
      </Subsection>
    )
  }

  const openPrompt = async (path: string) => {
    setBusy(true)
    setError(null)
    try {
      const loaded = await onGetDetail(path)
      if (loaded) {
        setDetail(loaded)
        setEditContent(loaded.content)
        setSavedContent(loaded.content)
      } else {
        setError('Could not load this prompt.')
      }
    } finally {
      setBusy(false)
    }
  }

  const closeEditor = () => {
    if (dirty && !window.confirm('Discard unsaved override edits?')) return
    setDetail(null)
    setEditContent('')
    setSavedContent('')
    setError(null)
  }

  const handleSave = async () => {
    if (!detail) return
    setBusy(true)
    setError(null)
    try {
      const ok = await onSaveOverride(detail.path, editContent)
      if (ok) {
        setSavedContent(editContent)
        setDetail({ ...detail, source: 'overridden', has_override: true })
      } else {
        setError('Could not save the override.')
      }
    } finally {
      setBusy(false)
    }
  }

  const handleRevert = async () => {
    if (!detail) return
    if (!window.confirm(`Revert "${detail.path}" to its bundled default?`)) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      const ok = await onDeleteOverride(detail.path)
      if (ok) {
        const bundled = detail.bundled_content ?? ''
        setDetail({
          ...detail,
          source: 'bundled',
          has_override: false,
          content: bundled,
        })
        setEditContent(bundled)
        setSavedContent(bundled)
      } else {
        setError('Could not revert the override.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Subsection
      title="Prompt overrides"
      hint="Override the markdown body of any bundled prompt. Saved per prompt; reverting restores the shipped default."
    >
      {detail ? (
        <div className="settings-prompt-editor">
          <div className="settings-prompt-editor__head">
            <div className="settings-field__text">
              <span className="settings-prompt-editor__title">{detail.path}</span>
              {detail.description ? (
                <span className="settings-field__hint">{detail.description}</span>
              ) : null}
            </div>
            <div className="settings-field__actions">
              <DetailActionButton
                label="Back to prompt list"
                variant="ghost"
                onClick={closeEditor}
              />
              {detail.has_override ? (
                <DetailActionButton
                  label="Revert to bundled default"
                  variant="destructive"
                  onClick={() => {
                    void handleRevert()
                  }}
                  disabled={busy}
                />
              ) : null}
              <DetailActionButton
                label={busy ? 'Saving…' : 'Save override'}
                variant="accent"
                onClick={() => {
                  void handleSave()
                }}
                disabled={busy || editContent === savedContent}
              />
            </div>
          </div>
          <div className="settings-code-surface">
            <CodeMirrorEditor
              content={editContent}
              language="markdown"
              ariaLabel="Prompt override content"
              editorId="settings-prompt-editor"
              onChange={setEditContent}
              onSave={() => {
                void handleSave()
              }}
            />
          </div>
          {error ? (
            <p className="settings-field__hint" role="alert">
              {error}
            </p>
          ) : null}
        </div>
      ) : (
        <>
          <SelectField
            label="Category"
            ariaLabel="Filter prompts by category"
            value={category}
            options={options}
            onChange={setCategory}
          />
          {filtered.length === 0 ? (
            <p className="settings-field__empty">No prompts in this category.</p>
          ) : (
            <ul className="settings-typed-list__items">
              {filtered.map((prompt) => (
                <li key={prompt.path}>
                  <button
                    type="button"
                    className="settings-prompt-row"
                    disabled={busy}
                    aria-label={`Edit prompt ${prompt.path}`}
                    onClick={() => {
                      void openPrompt(prompt.path)
                    }}
                  >
                    <span className="settings-prompt-row__path">
                      {prompt.path}
                    </span>
                    {prompt.description ? (
                      <span className="settings-field__hint">
                        {prompt.description}
                      </span>
                    ) : null}
                    <span className="settings-prompt-row__badge">
                      {prompt.has_override ? 'Overridden' : 'Bundled'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {error ? (
            <p className="settings-field__hint" role="alert">
              {error}
            </p>
          ) : null}
        </>
      )}
    </Subsection>
  )
}

interface TemplateGroupProps {
  content: string
  onSave?: (content: string) => Promise<{ ok: boolean; errors?: string[] }>
}

function TemplateGroup({ content, onSave }: TemplateGroupProps) {
  const [draft, setDraft] = useState(content)
  const [baseline, setBaseline] = useState(content)
  const [errors, setErrors] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const { showRestart, restartError, markRestartRequired, restartDaemon } =
    useDaemonRestart()

  // Re-sync from the latest fetched template (e.g. after an import refetch) by
  // adjusting state during render rather than in an effect — React's
  // recommended prop-change pattern, which avoids the extra commit + flash of
  // an effect-driven reset. The guard makes this converge in one re-render.
  if (content !== baseline) {
    setBaseline(content)
    setDraft(content)
  }

  const dirty = draft !== baseline
  useSectionDirtyGuard(dirty)

  if (!onSave) {
    return (
      <Subsection
        title="Full configuration template"
        hint="Edit the whole defaults-plus-overrides document as YAML."
      >
        <p className="settings-section__pending">
          The template editor is unavailable.
        </p>
      </Subsection>
    )
  }

  const handleSave = async () => {
    setSaving(true)
    setErrors([])
    const result = await onSave(draft)
    setSaving(false)
    if (result.ok) {
      setBaseline(draft)
      markRestartRequired()
    } else {
      setErrors(result.errors ?? ['Save failed.'])
    }
  }

  return (
    <Subsection
      title="Full configuration template"
      hint="Edit the whole defaults-plus-overrides document as YAML. Saving writes every change at once and requires a daemon restart to apply."
    >
      {showRestart ? (
        <div className="settings-restart-banner" role="status">
          <span>
            Saved to the database. Restart the daemon to apply the changes.
          </span>
          <DetailActionButton
            label="Restart now"
            variant="secondary"
            onClick={() => {
              void restartDaemon()
            }}
          />
        </div>
      ) : null}
      <div className="settings-code-surface">
        <CodeMirrorEditor
          content={draft}
          language="yaml"
          ariaLabel="Configuration template"
          editorId="settings-template-editor"
          onChange={setDraft}
          onSave={() => {
            void handleSave()
          }}
        />
      </div>
      {errors.map((message, index) => (
        <p key={index} className="settings-field__hint" role="alert">
          {message}
        </p>
      ))}
      {restartError ? (
        <p className="settings-field__hint" role="alert">
          {restartError}
        </p>
      ) : null}
      <div className="settings-field__actions">
        <DetailActionButton
          label={saving ? 'Saving…' : 'Save template'}
          variant="accent"
          onClick={() => {
            void handleSave()
          }}
          disabled={saving || !dirty}
        />
      </div>
    </Subsection>
  )
}

interface BackupRestoreGroupProps {
  onExport?: () => Promise<ConfigExportBundle | null>
  onImport?: (
    bundle: ImportBundle,
  ) => Promise<{ success: boolean; summary: string }>
}

function BackupRestoreGroup({ onExport, onImport }: BackupRestoreGroupProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<SurfaceStatus | null>(null)

  if (!onExport || !onImport) {
    return (
      <Subsection
        title="Backup & restore"
        hint="Export or import the full configuration bundle."
      >
        <p className="settings-section__pending">
          Backup and restore is unavailable.
        </p>
      </Subsection>
    )
  }

  const handleExport = async () => {
    setBusy(true)
    setStatus(null)
    try {
      const bundle = await onExport()
      if (!bundle) {
        setStatus({ ok: false, message: 'Export failed.' })
        return
      }
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {
        type: 'application/json',
      })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `gobby-config-${bundle.exported_at.slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
      setStatus({ ok: true, message: 'Configuration exported.' })
    } catch (err) {
      setStatus({
        ok: false,
        message: `Export failed. ${err instanceof Error ? err.message : String(err)}`,
      })
    } finally {
      setBusy(false)
    }
  }

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setBusy(true)
    setStatus(null)
    try {
      const parsed = JSON.parse(await file.text()) as Partial<ImportBundle>
      const result = await onImport({
        config_store: parsed.config_store,
        config: parsed.config,
        prompts: parsed.prompts,
      })
      setStatus({
        ok: result.success,
        message: result.success
          ? `Import complete. ${result.summary}`
          : `Import failed. ${result.summary}`,
      })
    } catch (err) {
      setStatus({
        ok: false,
        message: `Import failed. ${err instanceof Error ? err.message : String(err)}`,
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Subsection
      title="Backup & restore"
      hint="Export the config store, secret metadata, and prompt overrides as a JSON bundle, or import a bundle to restore them. Importing overwrites matching keys."
    >
      <div className="settings-field__actions">
        <DetailActionButton
          label="Export configuration"
          variant="secondary"
          onClick={() => {
            void handleExport()
          }}
          disabled={busy}
        />
        <DetailActionButton
          label="Import configuration"
          variant="secondary"
          onClick={() => fileInputRef.current?.click()}
          disabled={busy}
        />
      </div>
      <input
        ref={fileInputRef}
        type="file"
        accept=".json,application/json"
        aria-label="Import configuration file"
        className="sr-only"
        onChange={(event) => {
          void handleFileChange(event)
        }}
      />
      {status ? (
        <p
          className="settings-field__hint"
          role={status.ok ? 'status' : 'alert'}
        >
          {status.message}
        </p>
      ) : null}
    </Subsection>
  )
}

export function PromptsTemplatesSection() {
  const {
    prompts,
    promptCategories,
    getPromptDetail,
    savePromptOverride,
    deletePromptOverride,
    templateContent,
    saveTemplate,
    exportConfig,
    importConfig,
  } = useSettingsSectionContext()

  return (
    <SettingsSection sectionId="prompts-templates" ownedPaths={OWNED_PATHS}>
      {() => (
        <>
          <PromptOverridesGroup
            prompts={prompts ?? []}
            categories={promptCategories ?? {}}
            onGetDetail={getPromptDetail}
            onSaveOverride={savePromptOverride}
            onDeleteOverride={deletePromptOverride}
          />
          <TemplateGroup content={templateContent ?? ''} onSave={saveTemplate} />
          <BackupRestoreGroup onExport={exportConfig} onImport={importConfig} />
        </>
      )}
    </SettingsSection>
  )
}
