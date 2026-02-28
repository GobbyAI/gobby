import { useState, useEffect, useMemo, useCallback } from 'react'
import type { GobbySkill } from '../../hooks/useSkills'
import { Input } from '../chat/ui/Input'
import { Button } from '../chat/ui/Button'
import { Badge } from '../chat/ui/Badge'
import { ScrollArea } from '../chat/ui/ScrollArea'
import { cn } from '../../lib/utils'

interface SkillBrowserModalProps {
  onSendMessage: (content: string, injectContext: string) => void
  onClose: () => void
}

export function SkillBrowserModal({ onSendMessage, onClose }: SkillBrowserModalProps) {
  const [skills, setSkills] = useState<GobbySkill[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const fetchSkills = async () => {
      try {
        const resp = await fetch('/api/skills?enabled=true&limit=200')
        if (resp.ok) {
          const data = await resp.json()
          if (!cancelled) setSkills(data.skills || [])
        }
      } catch (e) {
        console.error('Failed to fetch skills:', e)
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    fetchSkills()
    return () => { cancelled = true }
  }, [])

  const filtered = useMemo(() => {
    if (!search) return skills
    const lower = search.toLowerCase()
    return skills.filter(
      (s) => s.name.toLowerCase().includes(lower) || s.description?.toLowerCase().includes(lower),
    )
  }, [skills, search])

  const selectedSkill = useMemo(
    () => skills.find((s) => s.id === selectedSkillId) ?? null,
    [skills, selectedSkillId],
  )

  const handleRun = useCallback(() => {
    if (selectedSkill) {
      onSendMessage(`Run skill: ${selectedSkill.name}`, selectedSkill.content || '')
      onClose()
    }
  }, [selectedSkill, onSendMessage, onClose])

  const sourceBadge = (skill: GobbySkill) => {
    if (skill.source === 'template') return <Badge variant="default">template</Badge>
    if (skill.source === 'project') return <Badge variant="info">project</Badge>
    if (skill.hub_name) return <Badge variant="success">hub</Badge>
    return <Badge variant="default">installed</Badge>
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0 bg-muted/30">
        <div className="flex items-center gap-2">
          <SkillsIcon />
          <h2 className="text-lg font-semibold text-foreground">Skills</h2>
          {!isLoading && (
            <span className="text-xs text-muted-foreground">({filtered.length})</span>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-muted-foreground hover:text-foreground transition-colors p-1.5 rounded-md hover:bg-muted"
          aria-label="Close"
        >
          <XIcon />
        </button>
      </div>

      {/* Mobile: stacked layout. Desktop: side-by-side */}
      <div className="flex flex-col md:flex-row flex-1 min-h-0">
        {/* Left panel: skill list */}
        <div className={cn(
          'flex flex-col min-h-0 border-border',
          selectedSkill ? 'hidden md:flex md:w-[40%] md:border-r' : 'w-full md:w-[40%] md:border-r',
        )}>
          <div className="p-3 border-b border-border shrink-0">
            <Input
              type="text"
              placeholder="Search skills..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="bg-muted/50"
            />
          </div>
          <ScrollArea className="flex-1">
            {isLoading ? (
              <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <SpinnerIcon />
                Loading skills...
              </div>
            ) : filtered.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">
                {search ? 'No skills match your search.' : 'No enabled skills found.'}
              </p>
            ) : (
              filtered.map((skill) => (
                <button
                  key={skill.id}
                  className={cn(
                    'w-full text-left px-3 py-2.5 text-sm transition-colors border-b border-border/30',
                    selectedSkillId === skill.id
                      ? 'bg-accent/15 text-foreground border-l-2 border-l-accent'
                      : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground',
                  )}
                  onClick={() => setSelectedSkillId(skill.id)}
                >
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-foreground text-sm">{skill.name}</span>
                    {sourceBadge(skill)}
                  </div>
                  {skill.description && (
                    <div className="text-xs opacity-60 truncate mt-0.5">{skill.description}</div>
                  )}
                </button>
              ))
            )}
          </ScrollArea>
        </div>

        {/* Right panel: skill preview */}
        <div className={cn(
          'flex flex-col min-h-0',
          selectedSkill ? 'flex-1' : 'hidden md:flex flex-1',
        )}>
          {!selectedSkill ? (
            <div className="flex-1 flex flex-col items-center justify-center text-muted-foreground text-sm gap-2 p-4">
              <SkillsIcon size={32} />
              <span>Select a skill to preview it</span>
            </div>
          ) : (
            <>
              {/* Mobile back button */}
              <button
                className="md:hidden flex items-center gap-1 px-3 py-2 text-sm text-accent hover:bg-muted/50 border-b border-border shrink-0"
                onClick={() => setSelectedSkillId(null)}
              >
                <ChevronLeftIcon />
                Back to list
              </button>

              <div className="px-4 py-3 border-b border-border shrink-0 bg-muted/20">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-semibold text-foreground">{selectedSkill.name}</span>
                  {sourceBadge(selectedSkill)}
                  {selectedSkill.always_apply && <Badge variant="warning">always-apply</Badge>}
                </div>
                {selectedSkill.description && (
                  <p className="text-sm text-muted-foreground mt-1">{selectedSkill.description}</p>
                )}
                {selectedSkill.version && (
                  <span className="text-xs text-muted-foreground">v{selectedSkill.version}</span>
                )}
              </div>

              <ScrollArea className="flex-1 px-4 py-3">
                <pre className="text-xs text-foreground whitespace-pre-wrap font-mono bg-muted/50 rounded-md p-3 border border-border/50">
                  {selectedSkill.content || '(no content)'}
                </pre>
              </ScrollArea>

              <div className="px-4 py-3 border-t border-border shrink-0 bg-muted/20">
                <Button variant="primary" onClick={handleRun}>
                  Run Skill
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function XIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function ChevronLeftIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  )
}

function SkillsIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-accent">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  )
}

function SpinnerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="animate-spin">
      <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="32" />
    </svg>
  )
}
