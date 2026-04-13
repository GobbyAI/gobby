import { useState, useCallback, useEffect, useRef } from 'react'

export interface FileEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
  extension?: string
}

export interface OpenFile {
  projectId: string
  path: string
  name: string
  content: string | null
  originalContent: string | null
  editContent: string | null
  language: string
  loading: boolean
  saving: boolean
  error: string | null
  dirty: boolean
  editing: boolean
  image: boolean
  binary: boolean
  mime_type: string
  size: number
}

export interface Project {
  id: string
  name: string
  repo_path: string
}

// Map file extensions to syntax highlighter language names
function extensionToLanguage(ext: string): string {
  const map: Record<string, string> = {
    '.js': 'javascript',
    '.jsx': 'jsx',
    '.ts': 'typescript',
    '.tsx': 'tsx',
    '.py': 'python',
    '.rb': 'ruby',
    '.rs': 'rust',
    '.go': 'go',
    '.java': 'java',
    '.c': 'c',
    '.cpp': 'cpp',
    '.h': 'c',
    '.hpp': 'cpp',
    '.cs': 'csharp',
    '.swift': 'swift',
    '.kt': 'kotlin',
    '.scala': 'scala',
    '.php': 'php',
    '.html': 'html',
    '.htm': 'html',
    '.css': 'css',
    '.scss': 'scss',
    '.less': 'less',
    '.json': 'json',
    '.yaml': 'yaml',
    '.yml': 'yaml',
    '.xml': 'xml',
    '.md': 'markdown',
    '.sql': 'sql',
    '.sh': 'bash',
    '.bash': 'bash',
    '.zsh': 'bash',
    '.fish': 'bash',
    '.ps1': 'powershell',
    '.r': 'r',
    '.lua': 'lua',
    '.perl': 'perl',
    '.pl': 'perl',
    '.toml': 'toml',
    '.ini': 'ini',
    '.cfg': 'ini',
    '.dockerfile': 'docker',
    '.graphql': 'graphql',
    '.proto': 'protobuf',
    '.zig': 'zig',
    '.dart': 'dart',
    '.ex': 'elixir',
    '.exs': 'elixir',
    '.erl': 'erlang',
    '.hs': 'haskell',
    '.vim': 'vim',
    '.diff': 'diff',
    '.patch': 'diff',
    '.makefile': 'makefile',
    '.cmake': 'cmake',
    '.tf': 'hcl',
    '.hcl': 'hcl',
  }
  return map[ext] || 'text'
}

// Special filenames that have a known language
function filenameToLanguage(name: string): string | null {
  const lower = name.toLowerCase()
  const map: Record<string, string> = {
    'dockerfile': 'docker',
    'makefile': 'makefile',
    'cmakelists.txt': 'cmake',
    '.gitignore': 'bash',
    '.dockerignore': 'bash',
    '.env': 'bash',
    '.env.local': 'bash',
    '.env.example': 'bash',
  }
  return map[lower] || null
}

function getBaseUrl(): string {
  // Use relative URLs: Vite proxy handles /api in dev, same-origin in production
  return ''
}

export interface GitStatus {
  branch: string | null
  files: Record<string, string>
}

export function useFiles() {
  const [projects, setProjects] = useState<Project[]>([])
  const [expandedDirs, setExpandedDirs] = useState<Map<string, FileEntry[]>>(new Map())
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set())
  const [openFiles, setOpenFiles] = useState<OpenFile[]>([])
  const [activeFileIndex, setActiveFileIndex] = useState<number>(-1)
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set())
  const [gitStatuses, setGitStatuses] = useState<Map<string, GitStatus>>(new Map())

  const baseUrl = getBaseUrl()

  // Refs to avoid stale closures in async callbacks
  const openFilesRef = useRef(openFiles)
  openFilesRef.current = openFiles
  const expandedDirsRef = useRef(expandedDirs)
  expandedDirsRef.current = expandedDirs

  const fetchProjects = useCallback(async () => {
    try {
      const res = await fetch(`${baseUrl}/api/files/projects`)
      if (res.ok) {
        const data = await res.json()
        setProjects(data)
      }
    } catch (e) {
      console.error('Failed to fetch projects:', e)
    }
  }, [baseUrl])

  const fetchGitStatus = useCallback(async (projectId: string) => {
    try {
      const res = await fetch(`${baseUrl}/api/files/git-status?project_id=${encodeURIComponent(projectId)}`)
      if (res.ok) {
        const data: GitStatus = await res.json()
        setGitStatuses(prev => new Map(prev).set(projectId, data))
      }
    } catch (e) {
      console.error('Failed to fetch git status:', e)
    }
  }, [baseUrl])

  const expandProject = useCallback(async (projectId: string) => {
    setExpandedProjects(prev => {
      const next = new Set(prev)
      if (next.has(projectId)) {
        next.delete(projectId)
        return next
      }
      next.add(projectId)
      return next
    })

    // Load root directory if not already loaded
    const key = `${projectId}:`
    if (!expandedDirsRef.current.has(key)) {
      setLoadingDirs(prev => new Set(prev).add(key))
      try {
        const [treeRes] = await Promise.all([
          fetch(`${baseUrl}/api/files/tree?project_id=${encodeURIComponent(projectId)}&path=`),
          fetchGitStatus(projectId),
        ])
        if (treeRes.ok) {
          const entries: FileEntry[] = await treeRes.json()
          setExpandedDirs(prev => new Map(prev).set(key, entries))
        }
      } catch (e) {
        console.error('Failed to load directory:', e)
      } finally {
        setLoadingDirs(prev => {
          const next = new Set(prev)
          next.delete(key)
          return next
        })
      }
    }
  }, [baseUrl, fetchGitStatus])

  const expandDir = useCallback(async (projectId: string, dirPath: string) => {
    const key = `${projectId}:${dirPath}`

    // Toggle if already expanded
    if (expandedDirsRef.current.has(key)) {
      setExpandedDirs(prev => {
        const next = new Map(prev)
        next.delete(key)
        return next
      })
      return
    }

    setLoadingDirs(prev => new Set(prev).add(key))
    try {
      const res = await fetch(
        `${baseUrl}/api/files/tree?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(dirPath)}`
      )
      if (res.ok) {
        const entries: FileEntry[] = await res.json()
        setExpandedDirs(prev => new Map(prev).set(key, entries))
      }
    } catch (e) {
      console.error('Failed to expand directory:', e)
    } finally {
      setLoadingDirs(prev => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }
  }, [baseUrl])

  const openFile = useCallback(async (projectId: string, path: string, name: string) => {
    // Check if already open (use ref to avoid stale closure)
    const current = openFilesRef.current
    const existingIndex = current.findIndex(f => f.projectId === projectId && f.path === path)
    if (existingIndex >= 0) {
      setActiveFileIndex(existingIndex)
      return
    }

    const ext = '.' + name.split('.').pop()?.toLowerCase()
    const language = filenameToLanguage(name) || extensionToLanguage(ext || '')

    // Add placeholder tab
    const newFile: OpenFile = {
      projectId,
      path,
      name,
      content: null,
      originalContent: null,
      editContent: null,
      language,
      loading: true,
      saving: false,
      error: null,
      dirty: false,
      editing: false,
      image: false,
      binary: false,
      mime_type: '',
      size: 0,
    }

    setOpenFiles(prev => [...prev, newFile])
    setActiveFileIndex(current.length)

    // Fetch content
    try {
      const res = await fetch(
        `${baseUrl}/api/files/read?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`
      )
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const data = await res.json()

      setOpenFiles(prev =>
        prev.map(f =>
          f.projectId === projectId && f.path === path
            ? {
                ...f,
                content: data.content,
                originalContent: data.content,
                editContent: data.content,
                loading: false,
                image: data.image,
                binary: data.binary,
                mime_type: data.mime_type,
                size: data.size,
              }
            : f
        )
      )
    } catch (e) {
      setOpenFiles(prev =>
        prev.map(f =>
          f.projectId === projectId && f.path === path
            ? { ...f, loading: false, error: String(e) }
            : f
        )
      )
    }
  }, [baseUrl])

  const closeFile = useCallback((index: number) => {
    setOpenFiles(prev => prev.filter((_, i) => i !== index))
    setActiveFileIndex(prev => {
      if (prev >= index) {
        return Math.max(0, prev - 1)
      }
      return prev
    })
  }, [])

  // Fetch projects on mount
  useEffect(() => {
    fetchProjects()
  }, [fetchProjects])

  const getImageUrl = useCallback((projectId: string, path: string) => {
    return `${baseUrl}/api/files/image?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`
  }, [baseUrl])

  /** Toggle edit mode for a file tab. No-op when already editing with unsaved
   *  changes — the user must explicitly save or cancel first. */
  const toggleEditing = useCallback((index: number) => {
    setOpenFiles(prev =>
      prev.map((f, i) => {
        if (i !== index) return f
        if (f.editing && f.dirty) return f
        return { ...f, editing: !f.editing, editContent: f.originalContent, dirty: false }
      })
    )
  }, [])

  const cancelEditing = useCallback((index: number) => {
    setOpenFiles(prev =>
      prev.map((f, i) =>
        i === index
          ? { ...f, editing: false, editContent: f.originalContent, dirty: false }
          : f
      )
    )
  }, [])

  const updateEditContent = useCallback((index: number, newContent: string) => {
    setOpenFiles(prev =>
      prev.map((f, i) =>
        i === index
          ? { ...f, editContent: newContent, dirty: newContent !== f.originalContent }
          : f
      )
    )
  }, [])

  const saveFile = useCallback(async (index: number) => {
    const file = openFilesRef.current[index]
    if (!file || !file.dirty || file.editContent === null) return

    setOpenFiles(prev =>
      prev.map((f, i) => (i === index ? { ...f, saving: true } : f))
    )

    try {
      const res = await fetch(`${baseUrl}/api/files/write`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: file.projectId,
          path: file.path,
          content: file.editContent,
        }),
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        throw new Error(err.detail || `HTTP ${res.status}`)
      }

      setOpenFiles(prev =>
        prev.map((f, i) =>
          i === index
            ? {
                ...f,
                content: f.editContent,
                originalContent: f.editContent,
                dirty: false,
                saving: false,
              }
            : f
        )
      )
    } catch (e) {
      setOpenFiles(prev =>
        prev.map((f, i) =>
          i === index ? { ...f, saving: false, error: String(e) } : f
        )
      )
    }
  }, [baseUrl])

  const fetchDiff = useCallback(async (projectId: string, path: string): Promise<string> => {
    try {
      const res = await fetch(
        `${baseUrl}/api/files/git-diff?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`
      )
      if (res.ok) {
        const data = await res.json()
        return data.diff || ''
      }
    } catch (e) {
      console.error('Failed to fetch diff:', e)
    }
    return ''
  }, [baseUrl])

  // Warn before unloading with unsaved changes
  useEffect(() => {
    const hasDirty = openFiles.some(f => f.dirty)
    if (!hasDirty) return
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [openFiles])

  return {
    projects,
    expandedDirs,
    expandedProjects,
    openFiles,
    activeFileIndex,
    loadingDirs,
    gitStatuses,
    fetchProjects,
    expandProject,
    expandDir,
    openFile,
    closeFile,
    setActiveFileIndex,
    getImageUrl,
    toggleEditing,
    cancelEditing,
    updateEditContent,
    saveFile,
    fetchGitStatus,
    fetchDiff,
  }
}
