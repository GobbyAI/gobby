import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import { useSkills } from '../../hooks/useSkills'
import type { GobbySkill } from '../../hooks/useSkills'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { SkillsFilters } from './SkillsFilters'
import { SkillsGrid } from './SkillsGrid'
import { SkillDetail } from './SkillDetail'
import { SkillForm } from './SkillForm'
import type { SkillFormData } from './SkillForm'
import { SkillHubBrowser } from './SkillHubBrowser'
import { SkillImportModal } from './SkillImportModal'
import { cn } from '../../lib/utils'
import {
  WORKFLOWS_PAGE_CLS,
  WORKFLOWS_TOOLBAR_CLS,
  WORKFLOWS_TOOLBAR_LEFT_CLS,
  WORKFLOWS_TOOLBAR_TITLE_CLS,
  WORKFLOWS_TOOLBAR_COUNT_CLS,
  WORKFLOWS_TOOLBAR_RIGHT_CLS,
  WORKFLOWS_TOOLBAR_BTN_CLS,
  WORKFLOWS_NEW_BTN_CLS,
  WORKFLOWS_FILTER_BAR_CLS,
  WORKFLOWS_FILTER_WRAPPER_CLS,
  WORKFLOWS_FILTER_BTN_CLS,
  WORKFLOWS_FILTER_BADGE_CLS,
  WORKFLOWS_FILTER_POPOVER_CLS,
  WORKFLOWS_FILTER_POPOVER_SECTION_CLS,
  WORKFLOWS_FILTER_POPOVER_LABEL_CLS,
  WORKFLOWS_FILTER_POPOVER_CHIPS_CLS,
  WORKFLOWS_FILTER_CHIP_CLS,
  WORKFLOWS_FILTER_CHIP_ACTIVE_CLS,
  WORKFLOWS_SEARCH_CLS,
  WORKFLOWS_CONTENT_CLS,
  WORKFLOWS_LOADING_CLS,
} from '../workflows/workflows-styles'

const ERROR_TOAST_CLS =
  'fixed right-5 top-[60px] z-[1000] cursor-pointer appearance-none rounded-md border-0 bg-[var(--color-error)] px-4 py-2 text-left text-[length:var(--text-base)] text-[var(--accent-foreground)] [animation:fadeIn_0.2s_ease]'

const VIEW_TOGGLE_CLS =
  'flex items-center gap-0.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-0.5'
const VIEW_BTN_CLS =
  'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent p-0 text-[var(--text-muted)] transition-[background-color,color] duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const VIEW_BTN_ACTIVE_CLS = 'bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:text-[var(--bg-primary)]'

type ActiveTab = 'installed' | 'hub'
type SourceFilter = 'installed' | 'project' | 'deleted'

const SOURCE_OPTIONS: { value: SourceFilter; label: string }[] = [
  { value: 'installed', label: 'Installed' },
  { value: 'project', label: 'Project' },
  { value: 'deleted', label: 'Deleted' },
]

export function SkillsPage() {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const {
    skills,
    stats,
    isLoading,
    filters,
    setFilters,
    createSkill,
    updateSkill,
    deleteSkill,
    toggleSkill,
    searchSkills,
    importSkill,
    exportSkill,
    restoreDefaults,
    scanSkill,
    refreshSkills,
    hubs,
    hubResults,
    hubErrors,
    fetchHubs,
    searchHub,
    installFromHub,
    moveToProject,
    moveToGlobal,
    restoreSkill,
  } = useSkills()

  const [activeTab, setActiveTab] = useState<ActiveTab>('installed')
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('installed')
  const [showForm, setShowForm] = useState(false)
  const [editSkill, setEditSkill] = useState<GobbySkill | null>(null)
  const [selectedSkill, setSelectedSkill] = useState<GobbySkill | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [searchText, setSearchText] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [sourceTypeFilter, setSourceTypeFilter] = useState<string | null>(null)
  const [installing, setInstalling] = useState<string | null>(null)

  const [showFilterPopover, setShowFilterPopover] = useState(false)
  const filterRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!showFilterPopover) return
    const handleMouseDown = (e: MouseEvent) => {
      if (filterRef.current && !filterRef.current.contains(e.target as Node)) {
        setShowFilterPopover(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [showFilterPopover])

  const showError = useCallback((msg: string) => {
    setErrorMessage(msg)
    setTimeout(() => setErrorMessage(null), 4000)
  }, [])

  const filteredSkills = useMemo(() => {
    let result = skills

    if (sourceFilter === 'installed') {
      result = result.filter(s => s.source === 'installed' && !s.deleted_at)
    } else if (sourceFilter === 'project') {
      result = result.filter(s => s.source === 'project' && !s.deleted_at)
    } else if (sourceFilter === 'deleted') {
      result = result.filter(s => s.deleted_at)
    }

    if (sourceTypeFilter) {
      result = result.filter(s => s.source_type === sourceTypeFilter)
    }

    if (searchText) {
      const q = searchText.toLowerCase()
      result = result.filter(s =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q)
      )
    }

    return result
  }, [skills, sourceFilter, searchText, sourceTypeFilter])

  const handleSourceFilter = useCallback((f: SourceFilter) => {
    setSourceFilter(f)
    setFilters(prev => ({
      ...prev,
      includeDeleted: f === 'deleted',
    }))
  }, [setFilters])

  const handleCreate = useCallback(() => {
    setEditSkill(null)
    setShowForm(true)
  }, [])

  const handleEdit = useCallback((skill: GobbySkill) => {
    setSelectedSkill(null)
    setEditSkill(skill)
    setShowForm(true)
  }, [])

  const handleSave = useCallback(async (data: SkillFormData) => {
    try {
      if (editSkill) {
        await updateSkill(editSkill.id, data)
      } else {
        await createSkill(data)
      }
      setShowForm(false)
      setEditSkill(null)
    } catch (e) {
      showError(e instanceof Error ? e.message : 'Failed to save skill')
    }
  }, [editSkill, createSkill, updateSkill, showError])

  const handleDelete = useCallback(async (skillId: string) => {
    if (!await confirm({ title: 'Delete skill?', confirmLabel: 'Delete', destructive: true })) return
    const ok = await deleteSkill(skillId)
    if (!ok) showError('Failed to delete skill')
    if (selectedSkill?.id === skillId) setSelectedSkill(null)
  }, [confirm, deleteSkill, showError, selectedSkill])

  const handleToggle = useCallback(async (skillId: string, enabled: boolean) => {
    const ok = await toggleSkill(skillId, enabled)
    if (!ok) showError('Failed to toggle skill')
  }, [toggleSkill, showError])

  const handleExport = useCallback(async (skillId: string) => {
    const result = await exportSkill(skillId)
    if (result) {
      const blob = new Blob([result.content], { type: 'text/markdown' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = result.filename || 'SKILL.md'
      a.click()
      URL.revokeObjectURL(url)
    }
  }, [exportSkill])

  const handleImport = useCallback(async (source: string) => {
    const result = await importSkill(source)
    if (!result || result.imported === 0) {
      throw new Error('No skills imported')
    }
  }, [importSkill])

  const handleRestore = useCallback(async () => {
    const result = await restoreDefaults()
    if (result) {
      refreshSkills()
    }
  }, [restoreDefaults, refreshSkills])

  const handleSearch = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value
    setSearchText(val)
    if (val.trim()) {
      searchSkills(val)
    } else {
      refreshSkills()
    }
  }, [searchSkills, refreshSkills])

  const handleCategoryChange = useCallback((cat: string | null) => {
    setFilters(prev => ({ ...prev, category: cat }))
  }, [setFilters])

  const handleClearFilters = useCallback(() => {
    setFilters(prev => ({ ...prev, category: null }))
    setSourceTypeFilter(null)
  }, [setFilters])

  const handleHubInstall = useCallback(async (hubName: string, slug: string) => {
    const key = `${hubName}/${slug}`
    setInstalling(key)
    try {
      await installFromHub(hubName, slug)
    } catch (e) {
      showError(e instanceof Error ? e.message : 'Install failed')
    } finally {
      setInstalling(null)
    }
  }, [installFromHub, showError])

  const handleMoveToProject = useCallback(async (skillId: string) => {
    const pid = window.prompt('Project ID to move to:')
    if (!pid) return
    const result = await moveToProject(skillId, pid)
    if (!result) showError('Failed to move skill to project')
  }, [moveToProject, showError])

  const handleMoveToGlobal = useCallback(async (skillId: string) => {
    const result = await moveToGlobal(skillId)
    if (!result) showError('Failed to move skill to global')
  }, [moveToGlobal, showError])

  const handleRestoreSkill = useCallback(async (skillId: string) => {
    const result = await restoreSkill(skillId)
    if (!result) showError('Failed to restore skill')
  }, [restoreSkill, showError])

  const activeFilterCount = sourceFilter !== 'installed' ? 1 : 0

  return (
    <main className={WORKFLOWS_PAGE_CLS}>
      {ConfirmDialogElement}
      {errorMessage && (
        <button
          type="button"
          className={ERROR_TOAST_CLS}
          onClick={() => setErrorMessage(null)}
          aria-label={`Dismiss error: ${errorMessage}`}
        >
          {errorMessage}
        </button>
      )}

      <div className={WORKFLOWS_TOOLBAR_CLS}>
        <div className={WORKFLOWS_TOOLBAR_LEFT_CLS}>
          <h1 className={WORKFLOWS_TOOLBAR_TITLE_CLS}>Skills</h1>
          <span className={WORKFLOWS_TOOLBAR_COUNT_CLS}>{stats?.total ?? 0}</span>
        </div>
        <div className={WORKFLOWS_TOOLBAR_RIGHT_CLS}>
          <div className={cn(VIEW_TOGGLE_CLS, activeTab === 'installed' && 'mr-2')}>
            <button
              className={cn(VIEW_BTN_CLS, activeTab === 'installed' && VIEW_BTN_ACTIVE_CLS)}
              onClick={() => setActiveTab('installed')}
              title="Library"
            >
              <LibraryIcon />
            </button>
            <button
              className={cn(VIEW_BTN_CLS, activeTab === 'hub' && VIEW_BTN_ACTIVE_CLS)}
              onClick={() => setActiveTab('hub')}
              title="Hub Browser"
            >
              <HubIcon />
            </button>
          </div>
          {activeTab === 'installed' && (
            <>
              <button className={WORKFLOWS_TOOLBAR_BTN_CLS} onClick={() => setShowImport(true)} title="Import">
                <ImportIcon />
              </button>
              <button className={WORKFLOWS_TOOLBAR_BTN_CLS} onClick={handleRestore} title="Restore Defaults">
                <RestoreIcon />
              </button>
              <button className={WORKFLOWS_NEW_BTN_CLS} onClick={handleCreate}>+ New</button>
            </>
          )}
        </div>
      </div>

      <div className={WORKFLOWS_FILTER_BAR_CLS}>
        {activeTab === 'installed' && (
          <input
            className={WORKFLOWS_SEARCH_CLS}
            type="text"
            value={searchText}
            onChange={handleSearch}
            placeholder="Search"
          />
        )}
        {activeTab === 'installed' && (
          <div className={WORKFLOWS_FILTER_WRAPPER_CLS} ref={filterRef}>
            <button
              type="button"
              className={WORKFLOWS_FILTER_BTN_CLS}
              onClick={() => setShowFilterPopover(v => !v)}
            >
              Filter
              {activeFilterCount > 0 && (
                <span className={WORKFLOWS_FILTER_BADGE_CLS}>{activeFilterCount}</span>
              )}
            </button>
            {showFilterPopover && (
              <div className={WORKFLOWS_FILTER_POPOVER_CLS}>
                <div className={WORKFLOWS_FILTER_POPOVER_SECTION_CLS}>
                  <div className={WORKFLOWS_FILTER_POPOVER_LABEL_CLS}>Source</div>
                  <div className={WORKFLOWS_FILTER_POPOVER_CHIPS_CLS}>
                    {SOURCE_OPTIONS.map(opt => (
                      <button
                        key={opt.value}
                        type="button"
                        className={cn(
                          WORKFLOWS_FILTER_CHIP_CLS,
                          sourceFilter === opt.value && WORKFLOWS_FILTER_CHIP_ACTIVE_CLS,
                        )}
                        onClick={() => handleSourceFilter(opt.value)}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {activeTab === 'installed' && (
        <>
          <SkillsFilters
            stats={stats}
            category={filters.category}
            sourceType={sourceTypeFilter}
            onCategoryChange={handleCategoryChange}
            onSourceTypeChange={setSourceTypeFilter}
            onClear={handleClearFilters}
          />

          <div className={WORKFLOWS_CONTENT_CLS}>
            {isLoading ? (
              <div className={WORKFLOWS_LOADING_CLS}>Loading skills...</div>
            ) : (
              <SkillsGrid
                skills={filteredSkills}

                onSelect={setSelectedSkill}
                onToggle={handleToggle}
                onEdit={handleEdit}
                onDelete={handleDelete}
                onExport={handleExport}
                onMoveToProject={handleMoveToProject}
                onMoveToGlobal={handleMoveToGlobal}
                onRestore={handleRestoreSkill}
              />
            )}
          </div>
        </>
      )}

      {activeTab === 'hub' && (
        <div className={WORKFLOWS_CONTENT_CLS}>
          <SkillHubBrowser
            hubs={hubs}
            hubResults={hubResults}
            hubErrors={hubErrors}
            onFetchHubs={fetchHubs}
            onSearch={searchHub}
            onInstall={handleHubInstall}
            installing={installing}
          />
        </div>
      )}

      {selectedSkill && (
        <SkillDetail
          skill={selectedSkill}
          onClose={() => setSelectedSkill(null)}
          onEdit={handleEdit}
          onExport={handleExport}
          onScan={scanSkill}
        />
      )}

      {showForm && (
        <SkillForm
          skill={editSkill}
          onSave={handleSave}
          onCancel={() => { setShowForm(false); setEditSkill(null) }}
        />
      )}

      {showImport && (
        <SkillImportModal
          onImport={handleImport}
          onClose={() => setShowImport(false)}
        />
      )}
    </main>
  )
}

function ImportIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  )
}

function RestoreIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
    </svg>
  )
}

function LibraryIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <line x1="3" y1="3" x2="11" y2="3" />
      <line x1="3" y1="7" x2="11" y2="7" />
      <line x1="3" y1="11" x2="11" y2="11" />
    </svg>
  )
}

function HubIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <circle cx="7" cy="7" r="5.5" />
      <line x1="7" y1="1.5" x2="7" y2="12.5" />
      <path d="M1.5 7h11" />
      <path d="M2.5 3.5Q7 5.5 11.5 3.5" />
      <path d="M2.5 10.5Q7 8.5 11.5 10.5" />
    </svg>
  )
}
