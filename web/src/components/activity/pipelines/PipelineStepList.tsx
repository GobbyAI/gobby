import { useEffect, useRef, useState } from 'react'
import { cn } from '../../../lib/utils'
import {
  ADD_BTN_CLS,
  ADD_CLS,
  ADD_DOT_CLS,
  ADD_DROPDOWN_CLS,
  ADD_OPTION_CLS,
  EMPTY_CLS,
  FIELD_CLS,
  FIELD_INPUT_CLS,
  FIELD_LABEL_CLS,
  FIELD_SELECT_CLS,
  SECTION_HEADER_CLS,
  STEP_ACTION_CLS,
  STEP_ACTION_DANGER_CLS,
  STEP_ACTIONS_CLS,
  STEP_BODY_CLS,
  STEP_CHEVRON_CLS,
  STEP_CLS,
  STEP_COUNT_CLS,
  STEP_HEADER_CLS,
  STEP_ID_CLS,
  STEP_PREVIEW_CLS,
  STEPS_CLS,
  STEPS_SIDEBAR_CLS,
  TYPE_BADGE_CLS,
} from './PipelineEditor.styles'
import type {
  PipelineStep,
  StepChangeHandler,
  StepType,
} from './PipelineEditor.types'
import {
  STEP_TYPES,
  detectStepType,
  getStepPreview,
  getTypeColor,
} from './pipelineStepModel'
import {
  ActivateWorkflowFields,
  CommonFields,
  ExecFields,
  InvokePipelineFields,
  McpFields,
  PromptFields,
} from './PipelineStepFields'

type PipelineStepListProps = {
  steps: PipelineStep[]
  expandedIndex: number | null
  inSidebar?: boolean
  onExpandedIndexChange: (index: number | null) => void
  onUpdateStep: (index: number, updates: Partial<PipelineStep>) => void
  onDeleteStep: (index: number) => void | Promise<void>
  onMoveStep: (index: number, direction: -1 | 1) => void
  onChangeStepType: (index: number, type: StepType) => void
  onAddStep: (type: StepType) => void
}

export function PipelineStepList({
  steps,
  expandedIndex,
  inSidebar,
  onExpandedIndexChange,
  onUpdateStep,
  onDeleteStep,
  onMoveStep,
  onChangeStepType,
  onAddStep,
}: PipelineStepListProps) {
  return (
    <div className={cn(STEPS_CLS, inSidebar && STEPS_SIDEBAR_CLS)}>
      <div className={SECTION_HEADER_CLS}>
        Steps
        <span className={STEP_COUNT_CLS}>{steps.length}</span>
      </div>

      {steps.length === 0 && (
        <div className={EMPTY_CLS}>No steps yet. Add one below.</div>
      )}

      {steps.map((step, index) => (
        <PipelineStepCard
          key={index}
          step={step}
          index={index}
          totalSteps={steps.length}
          expanded={expandedIndex === index}
          onToggle={() => onExpandedIndexChange(expandedIndex === index ? null : index)}
          onUpdate={(updates) => onUpdateStep(index, updates)}
          onDelete={() => void onDeleteStep(index)}
          onMove={(direction) => onMoveStep(index, direction)}
          onChangeType={(type) => onChangeStepType(index, type)}
        />
      ))}

      <AddStepButton onAdd={onAddStep} />
    </div>
  )
}

function PipelineStepCard({
  step,
  index,
  totalSteps,
  expanded,
  onToggle,
  onUpdate,
  onDelete,
  onMove,
  onChangeType,
}: {
  step: PipelineStep
  index: number
  totalSteps: number
  expanded: boolean
  onToggle: () => void
  onUpdate: StepChangeHandler
  onDelete: () => void
  onMove: (direction: -1 | 1) => void
  onChangeType: (type: StepType) => void
}) {
  const type = detectStepType(step)
  const typeColor = getTypeColor(type)

  return (
    <div className={STEP_CLS}>
      <button
        type="button"
        className={STEP_HEADER_CLS}
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <span
          className={TYPE_BADGE_CLS}
          style={{
            background: `color-mix(in srgb, ${typeColor} 12%, transparent)`,
            color: typeColor,
          }}
        >
          {type}
        </span>
        <span className={STEP_ID_CLS}>{step.id}</span>
        <span className={STEP_PREVIEW_CLS}>{getStepPreview(step)}</span>
        <span className={STEP_CHEVRON_CLS}>{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className={STEP_BODY_CLS}>
          <div className={STEP_ACTIONS_CLS}>
            <button
              type="button"
              className={STEP_ACTION_CLS}
              onClick={() => onMove(-1)}
              disabled={index === 0}
              title="Move up"
            >
              &uarr;
            </button>
            <button
              type="button"
              className={STEP_ACTION_CLS}
              onClick={() => onMove(1)}
              disabled={index === totalSteps - 1}
              title="Move down"
            >
              &darr;
            </button>
            <button
              type="button"
              className={cn(STEP_ACTION_CLS, STEP_ACTION_DANGER_CLS)}
              onClick={onDelete}
              title="Delete step"
            >
              Delete
            </button>
          </div>

          <label className={FIELD_CLS}>
            <span className={FIELD_LABEL_CLS}>Step ID</span>
            <input
              type="text"
              className={FIELD_INPUT_CLS}
              value={step.id}
              onChange={(e) => onUpdate({ id: e.target.value })}
            />
          </label>

          <label className={FIELD_CLS}>
            <span className={FIELD_LABEL_CLS}>Type</span>
            <select
              className={FIELD_SELECT_CLS}
              value={type}
              onChange={(e) => onChangeType(e.target.value as StepType)}
            >
              {STEP_TYPES.map((stepType) => (
                <option key={stepType.value} value={stepType.value}>
                  {stepType.label}
                </option>
              ))}
            </select>
          </label>

          {type === 'exec' && <ExecFields step={step} onChange={onUpdate} />}
          {type === 'prompt' && <PromptFields step={step} onChange={onUpdate} />}
          {type === 'mcp' && <McpFields step={step} onChange={onUpdate} />}
          {type === 'invoke_pipeline' && (
            <InvokePipelineFields step={step} onChange={onUpdate} />
          )}
          {type === 'activate_workflow' && (
            <ActivateWorkflowFields step={step} onChange={onUpdate} />
          )}

          <CommonFields step={step} type={type} onChange={onUpdate} />
        </div>
      )}
    </div>
  )
}

function AddStepButton({ onAdd }: { onAdd: (type: StepType) => void }) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function handlePointerDown(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  return (
    <div className={ADD_CLS} ref={containerRef}>
      <button
        type="button"
        className={ADD_BTN_CLS}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        + Add Step
      </button>
      {open && (
        <div className={ADD_DROPDOWN_CLS}>
          {STEP_TYPES.map((stepType) => (
            <button
              key={stepType.value}
              type="button"
              className={ADD_OPTION_CLS}
              onClick={() => {
                onAdd(stepType.value)
                setOpen(false)
              }}
            >
              <span className={ADD_DOT_CLS} style={{ background: stepType.color }} />
              {stepType.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
