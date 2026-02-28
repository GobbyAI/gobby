import { useState, useEffect } from 'react'

interface SkillInfo {
  name: string
  description?: string
}

interface AgentSkillsEditorProps {
  skills: string[]
  onSkillsChange: (skills: string[]) => void
  projectId?: string
}

export function AgentSkillsEditor({ skills, onSkillsChange, projectId }: AgentSkillsEditorProps) {
  const [availableSkills, setAvailableSkills] = useState<SkillInfo[]>([])
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    const params = projectId ? `?project_id=${projectId}` : ''
    fetch(`/api/skills${params}`)
      .then(r => r.json())
      .then(data => {
        setAvailableSkills((data.skills || []).map((s: SkillInfo) => ({
          name: s.name,
          description: s.description,
        })))
      })
      .catch(() => setAvailableSkills([]))
  }, [projectId])

  const addableSkills = availableSkills.filter(s => !skills.includes(s.name))

  return (
    <div className="agent-rules-editor">
      <div className="agent-rules-chips">
        {skills.map(name => (
          <span key={name} className="agent-rules-chip">
            {name}
            <button
              type="button"
              className="agent-rules-chip-remove"
              onClick={() => onSkillsChange(skills.filter(s => s !== name))}
              title={`Remove ${name}`}
            >
              &times;
            </button>
          </span>
        ))}
        {skills.length === 0 && !adding && (
          <span className="agent-rules-empty">No skills assigned</span>
        )}
      </div>
      {adding ? (
        <select
          className="agent-edit-input agent-rules-add-select"
          autoFocus
          value=""
          onChange={e => {
            if (e.target.value) {
              onSkillsChange([...skills, e.target.value])
              setAdding(false)
            }
          }}
          onBlur={() => setAdding(false)}
        >
          <option value="">Select skill...</option>
          {addableSkills.map(s => (
            <option key={s.name} value={s.name}>{s.name}</option>
          ))}
          {addableSkills.length === 0 && (
            <option disabled>No skills available</option>
          )}
        </select>
      ) : (
        <button
          type="button"
          className="agent-defs-btn agent-rules-add-btn"
          onClick={() => setAdding(true)}
          disabled={addableSkills.length === 0}
        >
          + Add Skill
        </button>
      )}
    </div>
  )
}
