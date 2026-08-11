import { useEffect, useState } from 'react'
import { Button } from '../ui/Button'
import { Chip } from '../ui/Chip'
import { NativeSelect } from '../ui/NativeSelect'
import { coarseHitAreaCls } from '../ui/controlStyles'

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
    const params = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
    fetch(`/api/skills${params}`)
      .then((response) => response.json())
      .then((data) => {
        setAvailableSkills(
          (data.skills || []).map((skill: SkillInfo) => ({
            name: skill.name,
            description: skill.description,
          })),
        )
      })
      .catch(() => setAvailableSkills([]))
  }, [projectId])

  const addableSkills = availableSkills.filter((skill) => !skills.includes(skill.name))

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {skills.map((name) => (
          <Chip key={name} className="gap-1 border border-border pl-2.5 pr-2 text-sm">
            {name}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              className={`${coarseHitAreaCls} min-h-0 w-auto px-0.5 text-base leading-none hover:text-[var(--color-error)]`}
              onClick={() => onSkillsChange(skills.filter((skill) => skill !== name))}
              title={`Remove ${name}`}
            >
              &times;
            </Button>
          </Chip>
        ))}
        {skills.length === 0 && !adding && (
          <span className="text-sm italic text-[var(--text-muted)]">No skills assigned</span>
        )}
      </div>
      {adding ? (
        <NativeSelect
          wrapperClassName="max-w-50"
          className="text-sm"
          aria-label="Select skill"
          autoFocus
          value=""
          onChange={(event) => {
            if (event.target.value) {
              onSkillsChange([...skills, event.target.value])
              setAdding(false)
            }
          }}
          onBlur={() => setAdding(false)}
        >
          <option value="">Select skill...</option>
          {addableSkills.map((skill) => (
            <option key={skill.name} value={skill.name}>
              {skill.name}
            </option>
          ))}
          {addableSkills.length === 0 && <option disabled>No skills available</option>}
        </NativeSelect>
      ) : (
        <Button
          type="button"
          size="sm"
          dense
          className={`${coarseHitAreaCls} self-start`}
          onClick={() => setAdding(true)}
          disabled={addableSkills.length === 0}
        >
          + Add Skill
        </Button>
      )}
    </div>
  )
}
