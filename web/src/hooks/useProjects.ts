import { useState, useCallback, useEffect, useMemo } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface ProjectWithStats {
  id: string
  name: string
  display_name: string
  repo_path: string | null
  github_url: string | null
  github_repo: string | null
  linear_team_id: string | null
  created_at: string
  updated_at: string
  session_count: number
  open_task_count: number
  last_activity_at: string | null
}

export type ProjectSubTab = 'overview' | 'code' | 'tasks' | 'sessions' | 'settings'

function getBaseUrl(): string {
  return ''
}

export function useProjects() {
  const [projects, setProjects] = useState<ProjectWithStats[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null)
  const [activeSubTab, setActiveSubTab] = useState<ProjectSubTab>('overview')
  const [searchText, setSearchText] = useState('')

  const baseUrl = getBaseUrl()

  const fetchProjects = useCallback(async () => {
    setIsLoading(true)
    try {
      const res = await fetch(`${baseUrl}/api/projects`)
      if (res.ok) {
        const data: ProjectWithStats[] = await res.json()
        setProjects(data)
      }
    } catch (e) {
      console.error('Failed to fetch projects:', e)
    } finally {
      setIsLoading(false)
    }
  }, [baseUrl])

  useEffect(() => {
    fetchProjects()
  }, [fetchProjects])

  // Real-time updates via WebSocket
  useWebSocketEvent(
    'project_event',
    useCallback(() => {
      fetchProjects()
    }, [fetchProjects]),
  )

  const selectedProject = useMemo(
    () => projects.find(p => p.id === selectedProjectId) ?? null,
    [projects, selectedProjectId]
  )

  const filteredProjects = useMemo(() => {
    if (!searchText.trim()) return projects
    const q = searchText.toLowerCase()
    return projects.filter(p =>
      p.display_name.toLowerCase().includes(q) ||
      (p.repo_path && p.repo_path.toLowerCase().includes(q)) ||
      (p.github_repo && p.github_repo.toLowerCase().includes(q))
    )
  }, [projects, searchText])

  const selectProject = useCallback((id: string) => {
    setSelectedProjectId(id)
    setActiveSubTab('overview')
  }, [])

  const deselectProject = useCallback(() => {
    setSelectedProjectId(null)
    setActiveSubTab('overview')
  }, [])

  const updateProject = useCallback(async (
    projectId: string,
    fields: Partial<Pick<ProjectWithStats, 'name' | 'github_url' | 'github_repo' | 'linear_team_id'>>
  ): Promise<boolean> => {
    try {
      const res = await fetch(`${baseUrl}/api/projects/${encodeURIComponent(projectId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields),
      })
      if (res.ok) {
        const updated: ProjectWithStats = await res.json()
        setProjects(prev => prev.map(p => p.id === projectId ? updated : p))
        return true
      }
    } catch (e) {
      console.error('Failed to update project:', e)
    }
    return false
  }, [baseUrl])

  const deleteProject = useCallback(async (projectId: string): Promise<boolean> => {
    try {
      const res = await fetch(`${baseUrl}/api/projects/${encodeURIComponent(projectId)}`, {
        method: 'DELETE',
      })
      if (res.ok) {
        setProjects(prev => prev.filter(p => p.id !== projectId))
        if (selectedProjectId === projectId) {
          setSelectedProjectId(null)
        }
        return true
      }
    } catch (e) {
      console.error('Failed to delete project:', e)
    }
    return false
  }, [baseUrl, selectedProjectId])

  // Aggregate stats
  const totalSessions = useMemo(
    () => projects.reduce((sum, p) => sum + p.session_count, 0),
    [projects]
  )
  const totalOpenTasks = useMemo(
    () => projects.reduce((sum, p) => sum + p.open_task_count, 0),
    [projects]
  )

  return {
    projects: filteredProjects,
    allProjects: projects,
    isLoading,
    selectedProject,
    selectedProjectId,
    activeSubTab,
    setActiveSubTab,
    searchText,
    setSearchText,
    selectProject,
    deselectProject,
    updateProject,
    deleteProject,
    refresh: fetchProjects,
    totalSessions,
    totalOpenTasks,
  }
}
