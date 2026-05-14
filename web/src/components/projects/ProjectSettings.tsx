import { useState, useCallback } from 'react'
import type { ProjectWithStats } from '../../hooks/useProjects'
import { cn } from '../../lib/utils'
import { Heading } from '../shared/Heading'

const SETTINGS_CLS = 'flex max-w-[600px] flex-col gap-6'
const SECTION_CLS = 'rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4'
const SECTION_DANGER_CLS = 'border-[var(--color-error)]'
const HEADING_CLS = 'm-0 mb-3 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const LABEL_CLS = 'mb-3 flex flex-col gap-1 text-[length:var(--text-sm)] text-[var(--text-muted)]'
const INPUT_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-base)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const ACTIONS_CLS = 'mt-1 flex items-center gap-3'
const SAVE_BTN_CLS =
  'cursor-pointer rounded-md border-0 bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-base)] font-medium text-[var(--bg-primary)] hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const MESSAGE_CLS = 'text-[length:var(--text-sm)]'
const MESSAGE_BG: Record<'success' | 'error', string> = {
  success: 'text-[var(--color-success-foreground)]',
  error: 'text-[var(--color-error)]',
}

const DESC_CLS = 'm-0 mb-3 text-[length:var(--text-sm)] text-[var(--text-muted)] [&_code]:rounded [&_code]:bg-[var(--bg-tertiary)] [&_code]:px-1.5 [&_code]:py-px [&_code]:font-mono'
const DELETE_BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--color-error)] bg-transparent px-3 py-1.5 text-[length:var(--text-base)] text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)] pointer-coarse:min-h-11'
const DELETE_BTN_CONFIRM_CLS =
  'border-[var(--color-error)] bg-[var(--color-error)] text-white hover:bg-[var(--color-error)]'

const RULES_CLS = 'flex flex-col gap-3'
const RULE_ROW_CLS = 'flex items-center gap-3 [&_button]:min-w-[96px]'

type ApprovalRuleRow = { id: string; value: string }

function createApprovalRuleRow(value = ''): ApprovalRuleRow {
  const id =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `project-approval-rule-${Date.now()}-${Math.random().toString(36).slice(2)}`
  return { id, value }
}

function toApprovalRuleRows(rules: string[]): ApprovalRuleRow[] {
  return rules.map((rule) => createApprovalRuleRow(rule))
}

interface ProjectSettingsProps {
  project: ProjectWithStats
  onSave: (fields: Record<string, string | string[] | null>) => Promise<boolean>
  onDelete: () => Promise<boolean>
}

export function ProjectSettings({ project, onSave, onDelete }: ProjectSettingsProps) {
  const [githubUrl, setGithubUrl] = useState(project.github_url ?? '')
  const [githubRepo, setGithubRepo] = useState(project.github_repo ?? '')
  const [linearTeamId, setLinearTeamId] = useState(project.linear_team_id ?? '')
  const [linearProjectId, setLinearProjectId] = useState(project.linear_project_id ?? '')
  const [approvalRules, setApprovalRules] = useState<ApprovalRuleRow[]>(
    () => toApprovalRuleRows(project.approval_rules ?? []),
  )
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  const isProtected = ['_personal', '_orphaned', '_migrated', 'gobby'].includes(project.name)

  const handleSave = useCallback(async () => {
    setSaving(true)
    setMessage(null)
    const ok = await onSave({
      github_url: githubUrl || null,
      github_repo: githubRepo || null,
      linear_team_id: linearTeamId || null,
      linear_project_id: linearProjectId || null,
      approval_rules: approvalRules.map((rule) => rule.value.trim()).filter(Boolean),
    })
    setSaving(false)
    setMessage(ok
      ? { type: 'success', text: 'Settings saved' }
      : { type: 'error', text: 'Failed to save settings' }
    )
    if (ok) setTimeout(() => setMessage(null), 3000)
  }, [approvalRules, githubUrl, githubRepo, linearTeamId, linearProjectId, onSave])

  const handleDelete = useCallback(async () => {
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    setDeleting(true)
    const ok = await onDelete()
    setDeleting(false)
    if (!ok) {
      setMessage({ type: 'error', text: 'Failed to delete project' })
      setConfirmDelete(false)
    }
  }, [confirmDelete, onDelete])

  return (
    <div className={SETTINGS_CLS}>
      <div className={SECTION_CLS}>
        <Heading level={3} className={HEADING_CLS}>Integrations</Heading>

        <label className={LABEL_CLS}>
          GitHub URL
          <input
            type="url"
            className={INPUT_CLS}
            value={githubUrl}
            onChange={e => setGithubUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
          />
        </label>

        <label className={LABEL_CLS}>
          GitHub Repo (owner/repo)
          <input
            type="text"
            className={INPUT_CLS}
            value={githubRepo}
            onChange={e => setGithubRepo(e.target.value)}
            placeholder="owner/repo"
          />
        </label>

        <label className={LABEL_CLS}>
          Linear Team ID
          <input
            type="text"
            className={INPUT_CLS}
            value={linearTeamId}
            onChange={e => setLinearTeamId(e.target.value)}
            placeholder="team-id"
          />
        </label>

        <label className={LABEL_CLS}>
          Linear Project ID
          <input
            type="text"
            className={INPUT_CLS}
            value={linearProjectId}
            onChange={e => setLinearProjectId(e.target.value)}
            placeholder="project-id"
          />
        </label>

        <div className={ACTIONS_CLS}>
          <button
            className={SAVE_BTN_CLS}
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
          {message && (
            <span className={cn(MESSAGE_CLS, MESSAGE_BG[message.type])}>
              {message.text}
            </span>
          )}
        </div>
      </div>

      {project.repo_path && (
        <div className={SECTION_CLS}>
          <Heading level={3} className={HEADING_CLS}>Tool Approvals</Heading>
          <p className={DESC_CLS}>
            Project-scoped auto-allow rules live in <code>.gobby/project.json</code>.
          </p>

          <div className={RULES_CLS}>
            {approvalRules.map((rule, index) => (
              <div key={rule.id} className={RULE_ROW_CLS}>
                <input
                  type="text"
                  className={cn(INPUT_CLS, 'flex-1')}
                  value={rule.value}
                  onChange={(e) =>
                    setApprovalRules((prev) =>
                      prev.map((ruleItem, i) =>
                        i === index ? { ...ruleItem, value: e.target.value } : ruleItem,
                      ),
                    )
                  }
                  placeholder="tool:Write or mcp:gobby-tasks:*"
                />
                <button
                  type="button"
                  className={DELETE_BTN_CLS}
                  onClick={() =>
                    setApprovalRules((prev) => prev.filter((_, i) => i !== index))
                  }
                >
                  Remove
                </button>
              </div>
            ))}
          </div>

          <div className={ACTIONS_CLS}>
            <button
              type="button"
              className={SAVE_BTN_CLS}
              onClick={() =>
                setApprovalRules((prev) => [...prev, createApprovalRuleRow('')])
              }
            >
              Add Rule
            </button>
          </div>
        </div>
      )}

      {!isProtected && (
        <div className={cn(SECTION_CLS, SECTION_DANGER_CLS)}>
          <Heading level={3} className={HEADING_CLS}>Danger Zone</Heading>
          <p className={DESC_CLS}>
            Deleting a project removes it from the list. Sessions and tasks remain in the database.
          </p>
          <button
            className={cn(DELETE_BTN_CLS, confirmDelete && DELETE_BTN_CONFIRM_CLS)}
            onClick={handleDelete}
            disabled={deleting}
          >
            {deleting ? 'Deleting...' : confirmDelete ? 'Click again to confirm' : 'Delete Project'}
          </button>
        </div>
      )}
    </div>
  )
}
