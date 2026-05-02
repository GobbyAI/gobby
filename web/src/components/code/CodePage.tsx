import { useState, useMemo } from 'react'
import { TabBar } from '../shared/TabBar'
import { FilesPage } from '../FilesPage'
import { CodeGraphExplorer } from '../code-graph/CodeGraphExplorer'
import { useFilesContext } from '../../contexts/useFilesContext'

interface CodePageProps {
  projectId: string | null
}

const TABS = [
  { id: 'editor', label: 'File Editor' },
  { id: 'graph', label: 'Code Graph' },
]

type CodeTab = 'editor' | 'graph'

export function CodePage({ projectId }: CodePageProps) {
  const [activeTab, setActiveTab] = useState<CodeTab>('editor')
  const files = useFilesContext()

  // Scope file tree to the selected project
  const scopedProjects = useMemo(
    () => projectId ? files.projects.filter(p => p.id === projectId) : files.projects,
    [projectId, files.projects]
  )

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="shrink-0 px-4">
        <TabBar
          tabs={TABS}
          activeTab={activeTab}
          onTabChange={(id) => setActiveTab(id as CodeTab)}
        />
      </div>
      <div className="relative flex flex-1 overflow-hidden">
        {activeTab === 'editor' && (
          scopedProjects.length > 0 ? (
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
          ) : (
            <div className="flex flex-1 items-center justify-center text-[length:var(--text-base)] text-[var(--text-muted)]">
              {projectId
                ? 'No files found for this project. Try refreshing or check the repo path.'
                : 'Select a project to browse files.'}
            </div>
          )
        )}
        {activeTab === 'graph' && (
          <CodeGraphExplorer projectId={projectId} />
        )}
      </div>
    </div>
  )
}
