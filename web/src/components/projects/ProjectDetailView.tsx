import type { ProjectWithStats, ProjectSubTab } from '../../hooks/useProjects'
import { ProjectSummary } from './ProjectSummary'
import { ProjectSettings } from './ProjectSettings'
import { cn } from '../../lib/utils'

const DETAIL_CLS = 'flex flex-1 flex-col overflow-hidden'
const HEADER_CLS = 'flex shrink-0 items-center gap-2 pb-3'
const BACK_CLS =
  'flex cursor-pointer items-center gap-1 border-0 bg-transparent px-0 py-0.5 text-[length:var(--text-base)] text-[var(--accent)] hover:underline pointer-coarse:min-h-11'
const SEPARATOR_CLS = 'text-[var(--text-muted)]'
const NAME_CLS = 'text-[length:var(--text-lg)] font-semibold text-[var(--text-primary)]'
const GITHUB_LINK_CLS = 'text-[var(--text-muted)] opacity-70 transition-opacity duration-150 hover:opacity-100'

const TABS_CLS = 'flex shrink-0 border-b border-[var(--border)]'
const TAB_CLS =
  'flex cursor-pointer items-center gap-1.5 border-0 border-b-2 border-b-transparent bg-transparent px-4 py-2 text-[length:var(--text-sm)] text-[var(--text-muted)] transition-all duration-150 hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const TAB_ACTIVE_CLS = 'border-b-[var(--accent)] text-[var(--accent)]'

const CONTENT_CLS = 'flex-1 overflow-y-auto pt-4'
const EMPTY_CLS = 'flex items-center justify-center p-12 text-[var(--text-muted)]'

interface ProjectDetailViewProps {
  project: ProjectWithStats
  activeTab: ProjectSubTab
  onTabChange: (tab: ProjectSubTab) => void
  onBack: () => void
  onSave: (fields: Record<string, string | string[] | null>) => Promise<boolean>
  onDelete: () => Promise<boolean>
  renderCodeTab?: () => React.ReactNode
}

const TABS: { key: ProjectSubTab; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'code', label: 'Code' },
  { key: 'settings', label: 'Settings' },
]

export function ProjectDetailView({
  project,
  activeTab,
  onTabChange,
  onBack,
  onSave,
  onDelete,
  renderCodeTab,
}: ProjectDetailViewProps) {
  return (
    <div className={DETAIL_CLS}>
      <div className={HEADER_CLS}>
        <button className={BACK_CLS} onClick={onBack}>
          <BackIcon /> Projects
        </button>
        <span className={SEPARATOR_CLS}>/</span>
        <span className={NAME_CLS}>{project.display_name}</span>
        {project.github_url && (
          <a
            href={project.github_url}
            target="_blank"
            rel="noopener noreferrer"
            className={GITHUB_LINK_CLS}
            title="Open on GitHub"
          >
            <GithubSmallIcon />
          </a>
        )}
      </div>

      <div className={TABS_CLS}>
        {TABS.map(tab => (
          <button
            key={tab.key}
            className={cn(TAB_CLS, activeTab === tab.key && TAB_ACTIVE_CLS)}
            onClick={() => onTabChange(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className={CONTENT_CLS}>
        {activeTab === 'overview' && <ProjectSummary project={project} />}
        {activeTab === 'code' && (
          renderCodeTab ? renderCodeTab() : (
            <div className={EMPTY_CLS}>
              {project.repo_path
                ? 'Loading code explorer...'
                : 'No repository path configured for this project.'}
            </div>
          )
        )}
        {activeTab === 'settings' && (
          <ProjectSettings
            project={project}
            onSave={onSave}
            onDelete={onDelete}
          />
        )}
      </div>
    </div>
  )
}

function BackIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  )
}

function GithubSmallIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
    </svg>
  )
}
