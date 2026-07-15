import { forwardRef, useCallback, useImperativeHandle, useMemo, useState } from 'react'
import { useConfirmDialog } from '../../../hooks/useConfirmDialog'
import { cn } from '../../../lib/utils'
import {
  BADGE_CLS,
  BACK_CLS,
  BTN_CLS,
  BTN_PRIMARY_CLS,
  DESCRIPTION_CLS,
  EDITOR_CLS,
  EDITOR_SIDEBAR_CLS,
  LABEL_CLS,
  META_CLS,
  NAME_CLS,
  SAVE_ERROR_CLS,
  TOOLBAR_CLS,
  TOOLBAR_LEFT_CLS,
  TOOLBAR_RIGHT_CLS,
} from './PipelineEditor.styles'
import type {
  PipelineEditorHandle,
  PipelineEditorProps,
  PipelineStep,
  StepType,
} from './PipelineEditor.types'
import { PipelineStepList } from './PipelineStepList'
import { changeStepPayload, createDefaultStep } from './pipelineStepModel'

export type { PipelineEditorHandle } from './PipelineEditor.types'

export const PipelineEditor = forwardRef<PipelineEditorHandle, PipelineEditorProps>(
  function PipelineEditor({ pipeline, updateWorkflow, onBack, onExport, inSidebar }, ref) {
    const { confirm, ConfirmDialogElement } = useConfirmDialog()

    const initDef = useMemo(() => {
      try {
        return JSON.parse(pipeline.definition_json) as Record<string, unknown>
      } catch {
        return {} as Record<string, unknown>
      }
    }, [pipeline.definition_json])

    const initSteps = useMemo(
      () => (Array.isArray(initDef.steps) ? (initDef.steps as PipelineStep[]) : []),
      [initDef],
    )

    const [name, setName] = useState(pipeline.name)
    const [description, setDescription] = useState(pipeline.description ?? '')
    const [steps, setSteps] = useState<PipelineStep[]>(initSteps)
    const [expandedIndex, setExpandedIndex] = useState<number | null>(null)
    const [saving, setSaving] = useState(false)
    const [isDirty, setDirty] = useState(false)
    const [loadedPipelineId, setLoadedPipelineId] = useState(pipeline.id)
    const [saveError, setSaveError] = useState<string | null>(null)

    // Reset editor state when a different pipeline is loaded into this instance.
    // Adjusting state during render matches existing codebase convention and
    // avoids an extra commit/flash.
    if (loadedPipelineId !== pipeline.id) {
      setLoadedPipelineId(pipeline.id)
      setName(pipeline.name)
      setDescription(pipeline.description ?? '')
      setSteps(initSteps)
      setExpandedIndex(null)
      setDirty(false)
      setSaveError(null)
    }

    const markDirty = useCallback(() => setDirty(true), [])

    const handleBack = useCallback(async () => {
      if (
        isDirty &&
        !(await confirm({
          title: 'Unsaved changes',
          description: 'You have unsaved changes. Discard them?',
          confirmLabel: 'Discard',
          destructive: true,
        }))
      ) {
        return
      }
      onBack()
    }, [isDirty, onBack, confirm])

    const updateStep = useCallback(
      (index: number, updates: Partial<PipelineStep>) => {
        setSteps((prev) => prev.map((step, i) => (i === index ? { ...step, ...updates } : step)))
        markDirty()
      },
      [markDirty],
    )

    const deleteStep = useCallback(
      async (index: number) => {
        if (
          !(await confirm({
            title: 'Delete step?',
            confirmLabel: 'Delete',
            destructive: true,
          }))
        ) {
          return
        }
        setSteps((prev) => prev.filter((_, i) => i !== index))
        setExpandedIndex(null)
        markDirty()
      },
      [markDirty, confirm],
    )

    const moveStep = useCallback(
      (index: number, direction: -1 | 1) => {
        setSteps((prev) => {
          const next = [...prev]
          const target = index + direction
          if (target < 0 || target >= next.length) return prev
          ;[next[index], next[target]] = [next[target], next[index]]
          return next
        })
        markDirty()
      },
      [markDirty],
    )

    const addStep = useCallback(
      (type: StepType) => {
        const ids = steps.map((step) => step.id)
        const step = createDefaultStep(type, ids)
        setSteps((prev) => [...prev, step])
        setExpandedIndex(steps.length)
        markDirty()
      },
      [steps, markDirty],
    )

    const changeStepType = useCallback(
      (index: number, newType: StepType) => {
        setSteps((prev) =>
          prev.map((step, i) => (i === index ? changeStepPayload(step, newType) : step)),
        )
        markDirty()
      },
      [markDirty],
    )

    const handleSave = useCallback(async () => {
      const ids = steps.map((step) => step.id)
      const dupes = ids.filter((id, i) => ids.indexOf(id) !== i)
      if (dupes.length > 0) {
        setSaveError(`Duplicate step IDs: ${dupes.join(', ')}`)
        return
      }

      setSaveError(null)
      setSaving(true)
      try {
        const def: Record<string, unknown> = { ...initDef }
        def.name = name.trim() || pipeline.name
        def.description = description.trim() || undefined
        def.steps = steps
        const saved = await updateWorkflow(pipeline.id, {
          name: name.trim() || pipeline.name,
          description: description.trim() || undefined,
          definition_json: JSON.stringify(def),
        })
        if (!saved) {
          setSaveError('Could not save the pipeline. Please try again.')
          return
        }
        setDirty(false)
      } catch (e) {
        setSaveError(`Save failed: ${e instanceof Error ? e.message : String(e)}`)
      } finally {
        setSaving(false)
      }
    }, [steps, name, description, initDef, pipeline, updateWorkflow])

    useImperativeHandle(
      ref,
      () => ({
        save: handleSave,
        isDirty,
      }),
      [handleSave, isDirty],
    )

    return (
      <div className={cn(EDITOR_CLS, inSidebar && EDITOR_SIDEBAR_CLS)}>
        {ConfirmDialogElement}
        {!inSidebar && (
          <div className={TOOLBAR_CLS}>
            <div className={TOOLBAR_LEFT_CLS}>
              <button type="button" className={BACK_CLS} onClick={handleBack}>
                &larr;
              </button>
              <input
                className={NAME_CLS}
                type="text"
                value={name}
                onChange={(e) => {
                  setName(e.target.value)
                  markDirty()
                }}
                placeholder="Pipeline name"
              />
              <span className={BADGE_CLS}>pipeline</span>
            </div>
            <div className={TOOLBAR_RIGHT_CLS}>
              <button type="button" className={BTN_CLS} onClick={onExport}>
                Export YAML
              </button>
              <button
                type="button"
                className={cn(BTN_CLS, BTN_PRIMARY_CLS)}
                onClick={handleSave}
                disabled={saving}
              >
                {saving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        )}

        {saveError && (
          <div className={SAVE_ERROR_CLS} role="alert">
            {saveError}
          </div>
        )}

        <label className={META_CLS}>
          <span className={LABEL_CLS}>Description</span>
          <textarea
            className={DESCRIPTION_CLS}
            value={description}
            onChange={(e) => {
              setDescription(e.target.value)
              markDirty()
            }}
            placeholder="Pipeline description..."
            rows={2}
          />
        </label>

        <PipelineStepList
          steps={steps}
          expandedIndex={expandedIndex}
          inSidebar={inSidebar}
          onExpandedIndexChange={setExpandedIndex}
          onUpdateStep={updateStep}
          onDeleteStep={deleteStep}
          onMoveStep={moveStep}
          onChangeStepType={changeStepType}
          onAddStep={addStep}
        />
      </div>
    )
  },
)
