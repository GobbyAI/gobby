import {
  AGENT_DEF_BADGE_CHIP_CLS,
  AGENT_DEF_BADGE_CHIP_LOCAL_CLS,
  AGENT_DEF_BADGE_CLS,
  AGENT_DEF_BADGE_DIM_CLS,
  AGENT_DEF_BADGE_FILLED_CLS,
  AGENT_DEF_BADGES_CLS,
  AGENT_DEF_CARD_CLS,
  AGENT_DEF_CARD_DELETED_CLS,
  AGENT_DEF_CARD_FOOTER_PAD_CLS,
  AGENT_DEF_DESC_CLS,
  AGENT_DEF_HEADER_CLS,
  AGENT_DEF_HEADER_TOP_CLS,
  AGENT_DEF_IMPORT_RESULT_CLS,
  AGENT_DEF_IMPORT_RESULT_ERR_CLS,
  AGENT_DEF_IMPORT_RESULT_OK_CLS,
  AGENT_DEF_NAME_CLS,
  AGENT_DEF_NAME_DELETED_CLS,
} from '../agents/agents-styles'
import { getProviderColorVar } from '../shared/sourceTheme'
import {
  WORKFLOWS_ACTION_BTN_CLS,
  WORKFLOWS_ACTION_BTN_DRIFT_CLS,
  WORKFLOWS_ACTION_BTN_RESTORE_CLS,
  WORKFLOWS_ACTION_ICON_CLS,
  WORKFLOWS_ACTION_ICON_DANGER_CLS,
  WORKFLOWS_CARD_ACTIONS_CLS,
  WORKFLOWS_CARD_BADGE_CLS,
  WORKFLOWS_CARD_BADGE_DRIFT_CLS,
  WORKFLOWS_CARD_BADGE_SOURCE_CLS,
  WORKFLOWS_CARD_FOOTER_CLS,
  WORKFLOWS_CARD_TEMPLATE_CLS,
  WORKFLOWS_CARD_TYPE_CLS,
  WORKFLOWS_CARD_TYPE_VARIANT_CLS,
  WORKFLOWS_CONTENT_CLS,
  WORKFLOWS_EMPTY_CLS,
  WORKFLOWS_GRID_CLS,
  WORKFLOWS_LOADING_CLS,
} from './workflows-styles'
import type { AgentDefInfo } from './AgentsTab.types'
import { SOURCE_LABELS } from './AgentsTab.types'
import { getIsolationColorVar } from './isolationColors'

export interface AgentImportResult {
  name: string
  ok: boolean
}

interface AgentDefinitionsGridProps {
  loading: boolean
  definitions: AgentDefInfo[]
  devMode: boolean
  installedNames: Set<string>
  importingName: string | null
  importResult: AgentImportResult | null
  projectId?: string
  onOpenAgent: (item: AgentDefInfo) => void
  onDuplicate: (item: AgentDefInfo) => void
  onDelete: (dbId: string) => void
  onRestore: (dbId: string) => void
  onDownload: (name: string) => void
  onInstallFromTemplate: (name: string) => void
  onMoveToProject: (item: AgentDefInfo) => void
  onMoveToGlobal: (item: AgentDefInfo) => void
  onRestoreFromTemplate: (item: AgentDefInfo) => void
  onImport: (name: string) => void
}

interface AgentDefinitionCardProps extends Omit<
  AgentDefinitionsGridProps,
  'loading' | 'definitions'
> {
  item: AgentDefInfo
}

const duplicateIcon = (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <rect x="5.5" y="5.5" width="9" height="9" rx="1.5" />
    <path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" />
  </svg>
)

const downloadIcon = (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" />
  </svg>
)

const deleteIcon = (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" />
    <path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" />
  </svg>
)

export function AgentDefinitionsGrid({
  loading,
  definitions,
  devMode,
  installedNames,
  importingName,
  importResult,
  projectId,
  onOpenAgent,
  onDuplicate,
  onDelete,
  onRestore,
  onDownload,
  onInstallFromTemplate,
  onMoveToProject,
  onMoveToGlobal,
  onRestoreFromTemplate,
  onImport,
}: AgentDefinitionsGridProps) {
  return (
    <div className={WORKFLOWS_CONTENT_CLS}>
      {loading ? (
        <div className={WORKFLOWS_LOADING_CLS}>Loading agent definitions...</div>
      ) : definitions.length === 0 ? (
        <div className={WORKFLOWS_EMPTY_CLS}>No agent definitions found</div>
      ) : (
        <div className={WORKFLOWS_GRID_CLS}>
          {definitions.map((item) => (
            <AgentDefinitionCard
              key={item.definition.name}
              item={item}
              devMode={devMode}
              installedNames={installedNames}
              importingName={importingName}
              importResult={importResult}
              projectId={projectId}
              onOpenAgent={onOpenAgent}
              onDuplicate={onDuplicate}
              onDelete={onDelete}
              onRestore={onRestore}
              onDownload={onDownload}
              onInstallFromTemplate={onInstallFromTemplate}
              onMoveToProject={onMoveToProject}
              onMoveToGlobal={onMoveToGlobal}
              onRestoreFromTemplate={onRestoreFromTemplate}
              onImport={onImport}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function AgentDefinitionCard({
  item,
  devMode,
  installedNames,
  importingName,
  importResult,
  projectId,
  onOpenAgent,
  onDuplicate,
  onDelete,
  onRestore,
  onDownload,
  onInstallFromTemplate,
  onMoveToProject,
  onMoveToGlobal,
  onRestoreFromTemplate,
  onImport,
}: AgentDefinitionCardProps) {
  const d = item.definition
  const isTemplate = item.source === 'template'
  const wfMeta = ['rules', 'variables', 'pipeline']
  const workflowCount = d.workflows
    ? Object.entries(d.workflows).filter(([key]) => (
      !wfMeta.includes(key) &&
      typeof d.workflows![key] === 'object' &&
      d.workflows![key] !== null &&
      !Array.isArray(d.workflows![key])
    )).length
    : 0

  return (
    <div
      data-testid="agent-def-card"
      className={`${AGENT_DEF_CARD_CLS}${item.deleted_at ? ' ' + AGENT_DEF_CARD_DELETED_CLS : ''}${isTemplate ? ' ' + WORKFLOWS_CARD_TEMPLATE_CLS : ''}`}
    >
      <button
        className={AGENT_DEF_HEADER_CLS}
        onClick={() => {
          if (item.deleted_at) return
          onOpenAgent(item)
        }}
      >
        <div className={AGENT_DEF_HEADER_TOP_CLS}>
          <span className={`${AGENT_DEF_NAME_CLS}${item.deleted_at ? ' ' + AGENT_DEF_NAME_DELETED_CLS : ''}`}>{d.name}</span>
          <span className={`${WORKFLOWS_CARD_TYPE_CLS} ${WORKFLOWS_CARD_TYPE_VARIANT_CLS.agent}`}>agent</span>
        </div>
        {d.description && (
          <div className={AGENT_DEF_DESC_CLS}>
            {d.description.split('\n')[0].slice(0, 100)}
          </div>
        )}
        <div className={AGENT_DEF_BADGES_CLS}>
          <span className={`${WORKFLOWS_CARD_BADGE_CLS} ${WORKFLOWS_CARD_BADGE_SOURCE_CLS}`}>
            {SOURCE_LABELS[item.source] || item.source}
          </span>
          <span
            className={`${AGENT_DEF_BADGE_CLS} ${AGENT_DEF_BADGE_FILLED_CLS}`}
            style={{ background: getProviderColorVar(d.provider) }}
          >
            {d.provider}
          </span>
          {d.is_local && (
            <span
              data-testid="agent-local-chip"
              className={`${AGENT_DEF_BADGE_CHIP_CLS} ${AGENT_DEF_BADGE_CHIP_LOCAL_CLS}`}
            >
              LOCAL
            </span>
          )}
          {d.isolation && (
            <span
              className={`${AGENT_DEF_BADGE_CLS} ${AGENT_DEF_BADGE_FILLED_CLS}`}
              style={{ background: getIsolationColorVar(d.isolation) }}
            >
              {d.isolation}
            </span>
          )}
          {workflowCount > 0 && (
            <span className={`${AGENT_DEF_BADGE_CLS} ${AGENT_DEF_BADGE_DIM_CLS}`}>
              {workflowCount} workflow{workflowCount !== 1 ? 's' : ''}
            </span>
          )}
          <span className={`${AGENT_DEF_BADGE_CLS} ${AGENT_DEF_BADGE_DIM_CLS}`}>
            {d.timeout}s
          </span>
          {item.has_template_update && (
            <span className={`${WORKFLOWS_CARD_BADGE_CLS} ${WORKFLOWS_CARD_BADGE_DRIFT_CLS}`}>Template updated</span>
          )}
        </div>
      </button>

      <div className={`${WORKFLOWS_CARD_FOOTER_CLS} ${AGENT_DEF_CARD_FOOTER_PAD_CLS}`}>
        {item.deleted_at ? (
          <div className={WORKFLOWS_CARD_ACTIONS_CLS}>
            {item.db_id && (
              <button
                type="button"
                className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_RESTORE_CLS}`}
                onClick={() => onRestore(item.db_id!)}
                title="Restore this agent"
              >
                Restore
              </button>
            )}
          </div>
        ) : isTemplate ? (
          <TemplateCardActions
            item={item}
            devMode={devMode}
            installedNames={installedNames}
            onDuplicate={onDuplicate}
            onDelete={onDelete}
            onDownload={onDownload}
            onInstallFromTemplate={onInstallFromTemplate}
          />
        ) : (
          <InstalledCardActions
            item={item}
            importingName={importingName}
            importResult={importResult}
            projectId={projectId}
            onDuplicate={onDuplicate}
            onDelete={onDelete}
            onDownload={onDownload}
            onMoveToProject={onMoveToProject}
            onMoveToGlobal={onMoveToGlobal}
            onRestoreFromTemplate={onRestoreFromTemplate}
            onImport={onImport}
          />
        )}
      </div>
    </div>
  )
}

function TemplateCardActions({
  item,
  devMode,
  installedNames,
  onDuplicate,
  onDelete,
  onDownload,
  onInstallFromTemplate,
}: Pick<AgentDefinitionsGridProps,
  'devMode' | 'installedNames' | 'onDuplicate' | 'onDelete' | 'onDownload' | 'onInstallFromTemplate'
> & { item: AgentDefInfo }) {
  const name = item.definition.name
  return (
    <>
      <div />
      <div className={WORKFLOWS_CARD_ACTIONS_CLS}>
        {installedNames.has(name)
          ? <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} disabled title="Already installed">Installed</button>
          : <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} onClick={() => onInstallFromTemplate(name)} title="Create an installed copy">Install</button>}
        {devMode && (
          <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={() => onDuplicate(item)} title="Duplicate" aria-label="Duplicate agent">
            {duplicateIcon}
          </button>
        )}
        <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={() => onDownload(name)} title="Download YAML" aria-label="Download agent as YAML">
          {downloadIcon}
        </button>
        {devMode && item.db_id && (
          <button type="button" className={`${WORKFLOWS_ACTION_ICON_CLS} ${WORKFLOWS_ACTION_ICON_DANGER_CLS}`} onClick={() => onDelete(item.db_id!)} title="Delete" aria-label="Delete agent">
            {deleteIcon}
          </button>
        )}
      </div>
    </>
  )
}

function InstalledCardActions({
  item,
  importingName,
  importResult,
  projectId,
  onDuplicate,
  onDelete,
  onDownload,
  onMoveToProject,
  onMoveToGlobal,
  onRestoreFromTemplate,
  onImport,
}: Pick<AgentDefinitionsGridProps,
  | 'importingName'
  | 'importResult'
  | 'projectId'
  | 'onDuplicate'
  | 'onDelete'
  | 'onDownload'
  | 'onMoveToProject'
  | 'onMoveToGlobal'
  | 'onRestoreFromTemplate'
  | 'onImport'
> & { item: AgentDefInfo }) {
  const d = item.definition
  return (
    <>
      <div />
      <div className={WORKFLOWS_CARD_ACTIONS_CLS}>
        {item.source === 'installed' && projectId && item.db_id && d.name !== 'default' && (
          <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} onClick={() => onMoveToProject(item)} title="Move to current project">To Project</button>
        )}
        {item.source === 'project' && item.db_id && (
          <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} onClick={() => onMoveToGlobal(item)} title="Move to global scope">To Global</button>
        )}
        {item.has_template_update && item.db_id && (
          <button type="button" className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_DRIFT_CLS}`} onClick={() => onRestoreFromTemplate(item)} title="Restore to bundled template version">Restore</button>
        )}
        <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={() => onDuplicate(item)} title="Duplicate" aria-label="Duplicate agent">
          {duplicateIcon}
        </button>
        <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={() => onDownload(d.name)} title="Download YAML" aria-label="Download agent as YAML">
          {downloadIcon}
        </button>
        {item.db_id ? (
          <button type="button" className={`${WORKFLOWS_ACTION_ICON_CLS} ${WORKFLOWS_ACTION_ICON_DANGER_CLS}`} onClick={() => onDelete(item.db_id!)} title="Delete" aria-label="Delete agent">
            {deleteIcon}
          </button>
        ) : (
          <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} onClick={() => onImport(d.name)} disabled={importingName === d.name} title="Import to DB for customization">
            {importingName === d.name ? '...' : 'Import'}
          </button>
        )}
        {importResult?.name === d.name && (
          <span className={`${AGENT_DEF_IMPORT_RESULT_CLS} ${importResult.ok ? AGENT_DEF_IMPORT_RESULT_OK_CLS : AGENT_DEF_IMPORT_RESULT_ERR_CLS}`}>
            {importResult.ok ? 'OK' : 'Fail'}
          </span>
        )}
      </div>
    </>
  )
}
