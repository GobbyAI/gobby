export const CRON_RUN_SUCCESS_STATUSES = new Set(['success', 'completed'])
export const CRON_RUN_FAILURE_STATUSES = new Set(['failed', 'error'])
export const CRON_RUN_ACTIVE_STATUSES = new Set(['running'])

export type CronRunStatusKind = 'success' | 'failure' | 'running' | 'pending'

export function cronRunStatusKind(status: string): CronRunStatusKind {
  if (CRON_RUN_SUCCESS_STATUSES.has(status)) return 'success'
  if (CRON_RUN_FAILURE_STATUSES.has(status)) return 'failure'
  if (CRON_RUN_ACTIVE_STATUSES.has(status)) return 'running'
  return 'pending'
}
