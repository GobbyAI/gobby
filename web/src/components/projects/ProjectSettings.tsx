import { useState, useCallback } from 'react'
import type { ProjectWithStats } from '../../hooks/useProjects'

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
      approval_rules: approvalRules.map((rule) => rule.value.trim()).filter(Boolean),
    })
    setSaving(false)
    setMessage(ok
      ? { type: 'success', text: 'Settings saved' }
      : { type: 'error', text: 'Failed to save settings' }
    )
    if (ok) setTimeout(() => setMessage(null), 3000)
  }, [approvalRules, githubUrl, githubRepo, linearTeamId, onSave])

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
    <div className="projects-settings">
      <div className="projects-settings-section">
        <h3 className="projects-settings-heading">Integrations</h3>

        <label className="projects-settings-label">
          GitHub URL
          <input
            type="url"
            className="projects-settings-input"
            value={githubUrl}
            onChange={e => setGithubUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
          />
        </label>

        <label className="projects-settings-label">
          GitHub Repo (owner/repo)
          <input
            type="text"
            className="projects-settings-input"
            value={githubRepo}
            onChange={e => setGithubRepo(e.target.value)}
            placeholder="owner/repo"
          />
        </label>

        <label className="projects-settings-label">
          Linear Team ID
          <input
            type="text"
            className="projects-settings-input"
            value={linearTeamId}
            onChange={e => setLinearTeamId(e.target.value)}
            placeholder="team-id"
          />
        </label>

        <div className="projects-settings-actions">
          <button
            className="projects-settings-save"
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
          {message && (
            <span className={`projects-settings-message projects-settings-message--${message.type}`}>
              {message.text}
            </span>
          )}
        </div>
      </div>

      {project.repo_path && (
        <div className="projects-settings-section">
          <h3 className="projects-settings-heading">Tool Approvals</h3>
          <p className="projects-settings-desc">
            Project-scoped auto-allow rules live in <code>.gobby/project.json</code>.
          </p>

          <div className="projects-settings-rules">
            {approvalRules.map((rule, index) => (
              <div key={rule.id} className="projects-settings-rule-row">
                <input
                  type="text"
                  className="projects-settings-input"
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
                  className="projects-settings-delete"
                  onClick={() =>
                    setApprovalRules((prev) => prev.filter((_, i) => i !== index))
                  }
                >
                  Remove
                </button>
              </div>
            ))}
          </div>

          <div className="projects-settings-actions">
            <button
              type="button"
              className="projects-settings-save"
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
        <div className="projects-settings-section projects-settings-danger">
          <h3 className="projects-settings-heading">Danger Zone</h3>
          <p className="projects-settings-desc">
            Deleting a project removes it from the list. Sessions and tasks remain in the database.
          </p>
          <button
            className={`projects-settings-delete ${confirmDelete ? 'projects-settings-delete--confirm' : ''}`}
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
