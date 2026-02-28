import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import { useSkills } from '../../hooks/useSkills'
import type { GobbySkill } from '../../hooks/useSkills'
import { SkillsFilters } from './SkillsFilters'
import { SkillsGrid } from './SkillsGrid'
import { SkillDetail } from './SkillDetail'
import { SkillForm } from './SkillForm'
import type { SkillFormData } from './SkillForm'
import { SkillHubBrowser } from './SkillHubBrowser'
import { SkillImportModal } from './SkillImportModal'
import '../workflows/WorkflowsPage.css'
import './SkillsPage.css'

type ActiveTab = 'installed' | 'hub'
type SourceFilter = 'installed' | 'project' | 'templates' | 'deleted'

const SOURCE_OPTIONS: { value: SourceFilter; label: string }[] = [
  { value: 'installed', label: 'Installed' },
  { value: 'project', label: 'Project' },
  { value: 'templates', label: 'Templates' },
  { value: 'deleted', label: 'Deleted' },
]

export function SkillsPage() {
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
    fetchHubs,
    searchHub,
    installFromHub,
    installFromTemplate,
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

  // Click-outside to close popover
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

  // Compute installed names for template Install/Installed button
  const installedNames = useMemo(() => {
    const names = new Set<string>()
    for (const s of skills) {
      if (s.source === 'installed' && !s.deleted_at) {
        names.add(s.name)
      }
    }
    return names
  }, [skills])

  // Apply source filter + search + source type filter to skills
  const filteredSkills = useMemo(() => {
    let result = skills

    if (sourceFilter === 'installed') {
      result = result.filter(s => s.source === 'installed' && !s.deleted_at)
    } else if (sourceFilter === 'project') {
      result = result.filter(s => s.source === 'project' && !s.deleted_at)
    } else if (sourceFilter === 'templates') {
      result = result.filter(s => s.source === 'template' && !s.deleted_at)
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
      includeTemplates: f === 'templates',
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
    if (!window.confirm('Delete this skill?')) return
    const ok = await deleteSkill(skillId)
    if (!ok) showError('Failed to delete skill')
    if (selectedSkill?.id === skillId) setSelectedSkill(null)
  }, [deleteSkill, showError, selectedSkill])

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

  const handleInstallFromTemplate = useCallback(async (skillId: string) => {
    try {
      await installFromTemplate(skillId)
    } catch (e) {
      showError(e instanceof Error ? e.message : 'Install failed')
    }
  }, [installFromTemplate, showError])

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
    <main className="workflows-page">
      {errorMessage && (
        <div className="skills-error-toast" onClick={() => setErrorMessage(null)}>
          {errorMessage}
        </div>
      )}

      {/* Title row */}
      <div className="workflows-toolbar">
        <div className="workflows-toolbar-left">
          <h2 className="workflows-toolbar-title">Skills</h2>
          <span className="workflows-toolbar-count">{stats?.total ?? 0}</span>
        </div>
        <div className="workflows-toolbar-right">
          <div className="skills-view-toggle" style={{ marginRight: activeTab === 'installed' ? '8px' : '0' }}>
            <button
              className={`skills-view-btn ${activeTab === 'installed' ? 'skills-view-btn--active' : ''}`}
              onClick={() => setActiveTab('installed')}
              title="Library"
            >
              <LibraryIcon />
            </button>
            <button
              className={`skills-view-btn ${activeTab === 'hub' ? 'skills-view-btn--active' : ''}`}
              onClick={() => setActiveTab('hub')}
              title="Hub Browser"
            >
              <HubIcon />
            </button>
          </div>
          {activeTab === 'installed' && (
            <>
              <button className="workflows-toolbar-btn" onClick={() => setShowImport(true)} title="Import">
                <ImportIcon />
              </button>
              <button className="workflows-toolbar-btn" onClick={handleRestore} title="Restore Defaults">
                <RestoreIcon />
              </button>
              <button className="workflows-new-btn" onClick={handleCreate}>+ New</button>
            </>
          )}
        </div>
      </div>

      {/* Filter row */}
      <div className="workflows-filter-row">
        {activeTab === 'installed' && (
          <input
            className="workflows-search"
            type="text"
            value={searchText}
            onChange={handleSearch}
            placeholder="Search..."
          />
        )}
        {activeTab === 'installed' && (
          <div className="workflows-filter-wrapper" ref={filterRef}>
            <button
              type="button"
              className="workflows-filter-btn"
              onClick={() => setShowFilterPopover(v => !v)}
            >
              Filter
              {activeFilterCount > 0 && (
                <span className="workflows-filter-badge">{activeFilterCount}</span>
              )}
            </button>
            {showFilterPopover && (
              <div className="workflows-filter-popover">
                <div className="workflows-filter-popover-section">
                  <div className="workflows-filter-popover-label">Source</div>
                  <div className="workflows-filter-popover-chips">
                    {SOURCE_OPTIONS.map(opt => (
                      <button
                        key={opt.value}
                        type="button"
                        className={`workflows-filter-chip ${sourceFilter === opt.value ? 'workflows-filter-chip--active' : ''}`}
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

      {/* Installed View */}
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

          <div className="workflows-content">
            {isLoading ? (
              <div className="workflows-loading">Loading skills...</div>
            ) : (
              <SkillsGrid
                skills={filteredSkills}
                installedNames={installedNames}
                onSelect={setSelectedSkill}
                onToggle={handleToggle}
                onEdit={handleEdit}
                onDelete={handleDelete}
                onExport={handleExport}
                onInstallFromTemplate={handleInstallFromTemplate}
                onMoveToProject={handleMoveToProject}
                onMoveToGlobal={handleMoveToGlobal}
                onRestore={handleRestoreSkill}
              />
            )}
          </div>
        </>
      )}

      {/* Hub View */}
      {activeTab === 'hub' && (
        <div className="workflows-content">
          <SkillHubBrowser
            hubs={hubs}
            hubResults={hubResults}
            onFetchHubs={fetchHubs}
            onSearch={searchHub}
            onInstall={handleHubInstall}
            installing={installing}
          />
        </div>
      )}

      {/* Detail slide-out */}
      {selectedSkill && (
        <SkillDetail
          skill={selectedSkill}
          onClose={() => setSelectedSkill(null)}
          onEdit={handleEdit}
          onExport={handleExport}
          onScan={scanSkill}
        />
      )}

      {/* Create/Edit modal */}
      {showForm && (
        <SkillForm
          skill={editSkill}
          onSave={handleSave}
          onCancel={() => { setShowForm(false); setEditSkill(null) }}
        />
      )}

      {/* Import modal */}
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
