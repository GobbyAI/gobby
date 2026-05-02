import { agentIcon, formatAssigneeDisplay, inferAssigneeType } from './assigneeUtils'

const BADGE_CLS = 'inline-flex items-center gap-[3px] max-w-20 overflow-hidden text-[length:var(--text-2xs)] text-[var(--text-muted)]'
const ICON_CLS = 'shrink-0 text-[length:var(--text-xs)]'
const LABEL_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap'

export function AssigneeBadge({
  assignee,
  agentName,
}: {
  assignee: string | null
  agentName: string | null
}) {
  if (!assignee) return null

  const isJoint = assignee.includes('+')
  const display = formatAssigneeDisplay(assignee, agentName)
  const type = inferAssigneeType(assignee, agentName)

  return (
    <span className={BADGE_CLS} title={assignee}>
      <span className={ICON_CLS}>{isJoint ? '\u{1F91D}' : agentIcon(type)}</span>
      <span className={LABEL_CLS}>{display}</span>
    </span>
  )
}
