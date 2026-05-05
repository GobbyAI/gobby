import { useState, useEffect } from 'react'
import {
  AGENT_BTN_CLS,
  AGENT_EDIT_INPUT_CLS,
  AGENT_RULES_ADD_BTN_CLS,
  AGENT_RULES_ADD_SELECT_CLS,
  AGENT_RULES_CHIP_CLS,
  AGENT_RULES_CHIP_REMOVE_CLS,
  AGENT_RULES_CHIPS_CLS,
  AGENT_RULES_EDITOR_CLS,
  AGENT_RULES_EMPTY_CLS,
} from './agents-styles'

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
    <div className={AGENT_RULES_EDITOR_CLS}>
      <div className={AGENT_RULES_CHIPS_CLS}>
        {skills.map(name => (
          <span key={name} className={AGENT_RULES_CHIP_CLS}>
            {name}
            <button
              type="button"
              className={AGENT_RULES_CHIP_REMOVE_CLS}
              onClick={() => onSkillsChange(skills.filter(s => s !== name))}
              title={`Remove ${name}`}
            >
              &times;
            </button>
          </span>
        ))}
        {skills.length === 0 && !adding && (
          <span className={AGENT_RULES_EMPTY_CLS}>No skills assigned</span>
        )}
      </div>
      {adding ? (
        <select
          className={`${AGENT_EDIT_INPUT_CLS} ${AGENT_RULES_ADD_SELECT_CLS}`}
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
          className={`${AGENT_BTN_CLS} ${AGENT_RULES_ADD_BTN_CLS}`}
          onClick={() => setAdding(true)}
          disabled={addableSkills.length === 0}
        >
          + Add Skill
        </button>
      )}
    </div>
  )
}
