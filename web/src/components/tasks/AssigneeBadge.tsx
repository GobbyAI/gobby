import { agentIcon, formatAssigneeDisplay } from './assigneeUtils'

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
  const type = agentName ? 'agent' : 'session'

  return (
    <span className="assignee-badge" title={assignee}>
      <span className="assignee-badge-icon">{isJoint ? '\u{1F91D}' : agentIcon(type)}</span>
      <span className="assignee-badge-label">{display}</span>
    </span>
  )
}
