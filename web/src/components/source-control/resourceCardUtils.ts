export interface ResourceField {
  label: string
  value: string
  muted?: boolean
  code?: boolean
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  return isNaN(d.getTime()) ? '-' : d.toLocaleDateString()
}

export function worktreeToFields(wt: {
  worktree_path: string
  task_id: string | null
  agent_session_id: string | null
  created_at: string
}): ResourceField[] {
  const fields: ResourceField[] = [{ label: 'Path', value: wt.worktree_path, code: true }]
  if (wt.task_id) fields.push({ label: 'Task', value: wt.task_id })
  if (wt.agent_session_id) fields.push({ label: 'Session', value: wt.agent_session_id, muted: true })
  fields.push({ label: 'Created', value: formatDate(wt.created_at), muted: true })
  return fields
}

export function cloneToFields(clone: {
  clone_path: string
  remote_url: string | null
  task_id: string | null
  created_at: string
}): ResourceField[] {
  const fields: ResourceField[] = [{ label: 'Path', value: clone.clone_path, code: true }]
  if (clone.remote_url) fields.push({ label: 'Remote', value: clone.remote_url, muted: true })
  if (clone.task_id) fields.push({ label: 'Task', value: clone.task_id })
  fields.push({ label: 'Created', value: formatDate(clone.created_at), muted: true })
  return fields
}
