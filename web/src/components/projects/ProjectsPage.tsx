import { useState, useMemo, useCallback } from 'react'
import './ProjectsPage.css'
import { useProjects } from '../../hooks/useProjects'
import type { ProjectWithStats } from '../../hooks/useProjects'
import { useFiles } from '../../hooks/useFiles'
import { ProjectOverview } from './ProjectOverview'
import { ProjectCard } from './ProjectCard'
import { ProjectDetailView } from './ProjectDetailView'
import { FilesPage } from '../FilesPage'
import { TasksPage } from '../tasks/TasksPage'
import { SessionsPage } from '../sessions/SessionsPage'
import { useSessions } from '../../hooks/useSessions'

type OverviewFilter = 'total' | 'active' | 'tasks' | null
type ViewMode = 'cards' | 'list'

function CardsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <rect x="1" y="1" width="5" height="5" rx="1" />
      <rect x="8" y="1" width="5" height="5" rx="1" />
      <rect x="1" y="8" width="5" height="5" rx="1" />
      <rect x="8" y="8" width="5" height="5" rx="1" />
    </svg>
  )
}

function ListIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <line x1="3" y1="3" x2="11" y2="3" />
      <line x1="3" y1="7" x2="11" y2="7" />
      <line x1="3" y1="11" x2="11" y2="11" />
    </svg>
  )
}

export function ProjectsPage() {
  const {
    projects,
    allProjects,
    isLoading,
    selectedProject,
    activeSubTab,
    setActiveSubTab,
    searchText,
    setSearchText,
    selectProject,
    deselectProject,
    updateProject,
    deleteProject,
    refresh,
    totalSessions,
    totalOpenTasks,
  } = useProjects()

  const files = useFiles()
  const sessionsHook = useSessions()

  const [viewMode, setViewMode] = useState<ViewMode>('cards')
  const [overviewFilter, setOverviewFilter] = useState<OverviewFilter>(null)

  // Apply overview filter
  const displayProjects = useMemo(() => {
    if (!overviewFilter) return projects
    if (overviewFilter === 'active') return projects.filter(p => p.session_count > 0)
    if (overviewFilter === 'tasks') return projects.filter(p => p.open_task_count > 0)
    return projects
  }, [projects, overviewFilter])

  const handleSave = useCallback(async (fields: Record<string, string | null>) => {
    if (!selectedProject) return false
    return updateProject(selectedProject.id, fields)
  }, [selectedProject, updateProject])

  const handleDelete = useCallback(async () => {
    if (!selectedProject) return false
    return deleteProject(selectedProject.id)
  }, [selectedProject, deleteProject])

  // Render Code tab: FilesPage scoped to one project
  const renderCodeTab = useCallback(() => {
    if (!selectedProject) return null
    const scopedProjects = files.projects.filter(p => p.id === selectedProject.id)
    if (scopedProjects.length === 0) {
      return (
        <div className="projects-detail-empty">
          {selectedProject.repo_path
            ? 'Project not found in file explorer. Try refreshing.'
            : 'No repository path configured for this project.'}
        </div>
      )
    }
    return (
      <FilesPage
        projects={scopedProjects}
        expandedDirs={files.expandedDirs}
        expandedProjects={files.expandedProjects}
        openFiles={files.openFiles}
        activeFileIndex={files.activeFileIndex}
        loadingDirs={files.loadingDirs}
        gitStatuses={files.gitStatuses}
        onExpandProject={files.expandProject}
        onExpandDir={files.expandDir}
        onOpenFile={files.openFile}
        onCloseFile={files.closeFile}
        onSetActiveFile={files.setActiveFileIndex}
        getImageUrl={files.getImageUrl}
        onToggleEditing={files.toggleEditing}
        onCancelEditing={files.cancelEditing}
        onUpdateEditContent={files.updateEditContent}
        onSaveFile={files.saveFile}
        onFetchDiff={files.fetchDiff}
      />
    )
  }, [selectedProject, files])

  // Render Tasks tab: TasksPage with projectFilter
  const renderTasksTab = useCallback(() => {
    if (!selectedProject) return null
    return <TasksPage projectFilter={selectedProject.id} />
  }, [selectedProject])

  // Render Sessions tab: SessionsPage filtered by project
  const renderSessionsTab = useCallback(() => {
    if (!selectedProject) return null
    // Filter sessions to this project
    const projectSessions = sessionsHook.filteredSessions.filter(
      s => s.project_id === selectedProject.id
    )
    return (
      <SessionsPage
        sessions={projectSessions}
        filters={{ ...sessionsHook.filters, projectId: selectedProject.id }}
        onFiltersChange={sessionsHook.setFilters}
        isLoading={sessionsHook.isLoading}
      />
    )
  }, [selectedProject, sessionsHook])

  // Detail view
  if (selectedProject) {
    return (
      <main className="projects-page">
        <ProjectDetailView
          project={selectedProject}
          activeTab={activeSubTab}
          onTabChange={setActiveSubTab}
          onBack={deselectProject}
          onSave={handleSave}
          onDelete={handleDelete}
          renderCodeTab={renderCodeTab}
          renderTasksTab={renderTasksTab}
          renderSessionsTab={renderSessionsTab}
        />
      </main>
    )
  }

  // List view
  return (
    <main className="projects-page">
      {/* Toolbar */}
      <div className="projects-toolbar">
        <div className="projects-toolbar-left">
          <h2 className="projects-toolbar-title">Projects</h2>
          <span className="projects-toolbar-count">{allProjects.length}</span>
        </div>
        <div className="projects-toolbar-right">
          <div className="projects-view-toggle">
            <button
              className={`projects-view-btn ${viewMode === 'cards' ? 'active' : ''}`}
              onClick={() => setViewMode('cards')}
              title="Card view"
            >
              <CardsIcon />
            </button>
            <button
              className={`projects-view-btn ${viewMode === 'list' ? 'active' : ''}`}
              onClick={() => setViewMode('list')}
              title="List view"
            >
              <ListIcon />
            </button>
          </div>
          <input
            className="projects-search"
            type="text"
            placeholder="Search..."
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            className="projects-toolbar-btn"
            onClick={refresh}
            title="Refresh"
            disabled={isLoading}
          >
            &#x21bb;
          </button>
        </div>
      </div>

      {/* Overview cards */}
      <ProjectOverview
        projects={allProjects}
        totalSessions={totalSessions}
        totalOpenTasks={totalOpenTasks}
        activeFilter={overviewFilter}
        onFilter={f => setOverviewFilter(f as OverviewFilter)}
      />

      {/* Content */}
      <div className="projects-content">
        {isLoading && displayProjects.length === 0 ? (
          <div className="projects-loading">Loading projects...</div>
        ) : displayProjects.length === 0 ? (
          <div className="projects-empty">
            {searchText ? 'No projects match your search.' : 'No projects found.'}
          </div>
        ) : viewMode === 'cards' ? (
          <div className="projects-grid">
            {displayProjects.map(p => (
              <ProjectCard
                key={p.id}
                project={p}
                onClick={() => selectProject(p.id)}
              />
            ))}
          </div>
        ) : (
          <div className="projects-list">
            {displayProjects.map(p => (
              <ProjectListRow
                key={p.id}
                project={p}
                onClick={() => selectProject(p.id)}
              />
            ))}
          </div>
        )}
      </div>
    </main>
  )
}

function ProjectListRow({ project, onClick }: { project: ProjectWithStats; onClick: () => void }) {
  return (
    <button className="projects-list-row" onClick={onClick}>
      <span className="projects-list-name">{project.display_name}</span>
      <span className="projects-list-path">
        {project.repo_path?.replace(/^\/Users\/[^/]+\//, '~/') ?? ''}
      </span>
      <span className="projects-list-stat">{project.session_count} sessions</span>
      <span className="projects-list-stat">{project.open_task_count} tasks</span>
      <span className="projects-list-activity">
        {project.last_activity_at
          ? new Date(project.last_activity_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
          : '—'}
      </span>
    </button>
  )
}
