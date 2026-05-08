import type { GobbySkill } from '../../hooks/useSkills'
import { cn } from '../../lib/utils'
import { SOURCE_BADGE_CLS, SOURCE_BADGE_BG } from './styles'
import {
  WORKFLOWS_EMPTY_CLS,
  WORKFLOWS_GRID_CLS,
  WORKFLOWS_CARD_CLS,
  WORKFLOWS_CARD_DELETED_CLS,
  WORKFLOWS_CARD_HEADER_CLS,
  WORKFLOWS_CARD_NAME_CLS,
  WORKFLOWS_CARD_TYPE_CLS,
  WORKFLOWS_CARD_TYPE_VARIANT_CLS,
  WORKFLOWS_CARD_DESC_CLS,
  WORKFLOWS_CARD_BADGES_CLS,
  WORKFLOWS_CARD_BADGE_CLS,
  WORKFLOWS_CARD_FOOTER_CLS,
  WORKFLOWS_CARD_ACTIONS_CLS,
  WORKFLOWS_TOGGLE_CLS,
  WORKFLOWS_TOGGLE_TRACK_CLS,
  WORKFLOWS_TOGGLE_TRACK_ON_CLS,
  WORKFLOWS_TOGGLE_KNOB_CLS,
  WORKFLOWS_TOGGLE_KNOB_ON_CLS,
  WORKFLOWS_ACTION_BTN_CLS,
  WORKFLOWS_ACTION_BTN_RESTORE_CLS,
  WORKFLOWS_ACTION_ICON_CLS,
  WORKFLOWS_ACTION_ICON_DANGER_CLS,
} from '../workflows/workflows-styles'

interface SkillsGridProps {
  skills: GobbySkill[]
  projectId?: string
  onSelect: (skill: GobbySkill) => void
  onToggle: (skillId: string, enabled: boolean) => void
  onEdit: (skill: GobbySkill) => void
  onDelete: (skillId: string) => void
  onExport: (skillId: string) => void
  onMoveToProject: (skillId: string) => void
  onMoveToGlobal: (skillId: string) => void
  onRestore: (skillId: string) => void
}

function SourceBadge({ source }: { source: string | null }) {
  const s = source || 'unknown'
  return <span className={cn(SOURCE_BADGE_CLS, SOURCE_BADGE_BG[s] ?? SOURCE_BADGE_BG.unknown)}>{s}</span>
}

function getCategory(skill: GobbySkill): string | null {
  if (skill.metadata && typeof skill.metadata === 'object' && 'category' in skill.metadata) {
    return skill.metadata.category as string
  }
  return null
}

export function SkillsGrid({
  skills,
  projectId,
  onSelect,
  onToggle,
  onEdit,
  onDelete,
  onExport,
  onMoveToProject,
  onMoveToGlobal,
  onRestore,
}: SkillsGridProps) {
  if (skills.length === 0) {
    return (
      <div className={WORKFLOWS_EMPTY_CLS}>No skills match the current filters.</div>
    )
  }

  return (
    <div className={WORKFLOWS_GRID_CLS}>
      {skills.map(skill => (
        <SkillCard
          key={skill.id}
          skill={skill}
          projectId={projectId}
          onSelect={() => onSelect(skill)}
          onToggle={() => onToggle(skill.id, !skill.enabled)}
          onEdit={() => onEdit(skill)}
          onDelete={() => onDelete(skill.id)}
          onExport={() => onExport(skill.id)}
          onMoveToProject={() => onMoveToProject(skill.id)}
          onMoveToGlobal={() => onMoveToGlobal(skill.id)}
          onRestore={() => onRestore(skill.id)}
        />
      ))}
    </div>
  )
}

function SkillCard({ skill, projectId, onSelect, onToggle, onEdit, onDelete, onExport, onMoveToProject, onMoveToGlobal, onRestore }: {
  skill: GobbySkill
  projectId?: string
  onSelect: () => void
  onToggle: () => void
  onEdit: () => void
  onDelete: () => void
  onExport: () => void
  onMoveToProject: () => void
  onMoveToGlobal: () => void
  onRestore: () => void
}) {
  const isDeleted = !!skill.deleted_at
  const category = getCategory(skill)

  return (
    <div
      className={cn(WORKFLOWS_CARD_CLS, isDeleted && WORKFLOWS_CARD_DELETED_CLS)}
      onClick={onSelect}
    >
      <div className={WORKFLOWS_CARD_HEADER_CLS}>
        <span className={WORKFLOWS_CARD_NAME_CLS}>{skill.name}</span>
        <span className={`${WORKFLOWS_CARD_TYPE_CLS} ${WORKFLOWS_CARD_TYPE_VARIANT_CLS.skill}`}>skill</span>
      </div>

      {skill.description && (
        <div className={WORKFLOWS_CARD_DESC_CLS}>{skill.description}</div>
      )}

      <div className={WORKFLOWS_CARD_BADGES_CLS}>
        {skill.always_apply && <span className={WORKFLOWS_CARD_BADGE_CLS}>always</span>}
        <SourceBadge source={skill.source} />
        {category && <span className={WORKFLOWS_CARD_BADGE_CLS}>{category}</span>}
        {skill.version && <span className={WORKFLOWS_CARD_BADGE_CLS}>v{skill.version}</span>}
        {skill.injection_format && skill.injection_format !== 'summary' && (
          <span className={WORKFLOWS_CARD_BADGE_CLS}>{skill.injection_format}</span>
        )}
      </div>

      <div className={WORKFLOWS_CARD_FOOTER_CLS}>
        {isDeleted ? (
          <>
            <div />
            <div className={WORKFLOWS_CARD_ACTIONS_CLS}>
              <button type="button" className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_RESTORE_CLS}`} onClick={e => { e.stopPropagation(); onRestore() }} title="Restore deleted skill">Restore</button>
              <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={e => { e.stopPropagation(); onExport() }} title="Export" aria-label="Export skill">
                <DownloadIcon />
              </button>
            </div>
          </>
        ) : (
          <>
            <div
              className={WORKFLOWS_TOGGLE_CLS}
              onClick={e => { e.stopPropagation(); onToggle() }}
            >
              <div className={`${WORKFLOWS_TOGGLE_TRACK_CLS} ${skill.enabled ? WORKFLOWS_TOGGLE_TRACK_ON_CLS : ''}`}>
                <div className={`${WORKFLOWS_TOGGLE_KNOB_CLS} ${skill.enabled ? WORKFLOWS_TOGGLE_KNOB_ON_CLS : ''}`} />
              </div>
              <span>{skill.enabled ? 'On' : 'Off'}</span>
            </div>

            <div className={WORKFLOWS_CARD_ACTIONS_CLS}>
              {skill.source === 'installed' && projectId && (
                <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} onClick={e => { e.stopPropagation(); onMoveToProject() }} title="Move to current project">To Project</button>
              )}
              {skill.source === 'project' && (
                <button type="button" className={WORKFLOWS_ACTION_BTN_CLS} onClick={e => { e.stopPropagation(); onMoveToGlobal() }} title="Move to global scope">To Global</button>
              )}
              <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={e => { e.stopPropagation(); onEdit() }} title="Edit skill" aria-label="Edit skill">
                <EditIcon />
              </button>
              <button type="button" className={WORKFLOWS_ACTION_ICON_CLS} onClick={e => { e.stopPropagation(); onExport() }} title="Export" aria-label="Export skill">
                <DownloadIcon />
              </button>
              <button type="button" className={`${WORKFLOWS_ACTION_ICON_CLS} ${WORKFLOWS_ACTION_ICON_DANGER_CLS}`} onClick={e => { e.stopPropagation(); onDelete() }} title="Delete skill" aria-label="Delete skill">
                <DeleteIcon />
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function EditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
    </svg>
  )
}

function DeleteIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" />
      <path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" />
    </svg>
  )
}
