import { useEffect, useRef, useState } from 'react'
import type { LifecycleTask, StageAdvanceAction } from '../../lib/stageActions'
import { cn } from '../../lib/utils'
import { lifecycleBoardStyles } from './lifecycleBoardStyles'
import type { Swimlane } from './lifecycleBoardModel'
import { tasksForStage } from './lifecycleBoardModel'
import { StageColumn, type StageRegistryEntry } from './StageColumn'

interface LifecycleLaneProps {
  lane: Swimlane
  stages: StageRegistryEntry[]
  onSelectTask: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
  onMoveTaskToStage?: (taskId: string, targetStageName: string) => void | Promise<void>
}

export function LifecycleLane({
  lane,
  stages,
  onSelectTask,
  onAdvanceStage,
  onMoveTaskToStage,
}: LifecycleLaneProps) {
  const columnsRef = useRef<HTMLDivElement | null>(null)
  const [activeStage, setActiveStage] = useState('')
  const firstStageName = stages[0]?.name ?? ''
  const selectedStageName = stages.some(stage => stage.name === activeStage)
    ? activeStage
    : firstStageName

  useEffect(() => {
    const root = columnsRef.current
    if (!root || !stages.length || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      entries => {
        const best = entries
          .filter(entry => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0]
        const stageName = best?.target.getAttribute('data-stage-name')
        if (stageName) setActiveStage(stageName)
      },
      { root, threshold: [0.5, 0.75] },
    )
    root.querySelectorAll<HTMLElement>('[data-stage-name]').forEach(element => {
      observer.observe(element)
    })
    return () => observer.disconnect()
  }, [stages])

  const scrollToStage = (stageName: string) => {
    const root = columnsRef.current
    const target = root?.querySelector<HTMLElement>(`[data-stage-name="${stageName}"]`)
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    target?.scrollIntoView({
      behavior: reducedMotion ? 'auto' : 'smooth',
      block: 'nearest',
      inline: 'start',
    })
    setActiveStage(stageName)
  }

  return (
    <section className={lifecycleBoardStyles.lane} role="rowgroup" aria-label={lane.label}>
      <header className={lifecycleBoardStyles.laneHeader}>
        <span>{lane.label}</span>
        <span>{lane.tasks.length}</span>
      </header>
      <nav className={lifecycleBoardStyles.lanePager} aria-label={`${lane.label} stages`}>
        {stages.map(stage => (
          <button
            key={stage.name}
            type="button"
            className={cn(
              lifecycleBoardStyles.lanePagerButton,
              selectedStageName === stage.name && lifecycleBoardStyles.lanePagerButtonActive,
            )}
            aria-current={selectedStageName === stage.name ? 'true' : undefined}
            onClick={() => scrollToStage(stage.name)}
          >
            {stage.display_name}
          </button>
        ))}
      </nav>
      <div ref={columnsRef} className={lifecycleBoardStyles.columns}>
        {stages.map(stage => (
          <StageColumn
            key={stage.name}
            stage={stage}
            tasks={tasksForStage(lane.tasks as LifecycleTask[], stage.name)}
            onSelectTask={onSelectTask}
            onAdvanceStage={onAdvanceStage}
            onMoveTaskToStage={onMoveTaskToStage}
            availableStages={stages}
            showDoneCardsWhenCollapsed
          />
        ))}
      </div>
    </section>
  )
}
