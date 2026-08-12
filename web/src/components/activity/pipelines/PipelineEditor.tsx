import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useMemo,
  useState,
} from "react";
import { useConfirmDialog } from "../../../hooks/useConfirmDialog";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { FormField } from "../../ui/FormField";
import { Input } from "../../ui/Input";
import { Textarea } from "../../ui/Textarea";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import type {
  PipelineEditorHandle,
  PipelineEditorProps,
  PipelineStep,
  StepType,
} from "./PipelineEditor.types";
import { PipelineStepList } from "./PipelineStepList";
import { changeStepPayload, createDefaultStep } from "./pipelineStepModel";

export type { PipelineEditorHandle } from "./PipelineEditor.types";

export const PipelineEditor = forwardRef<
  PipelineEditorHandle,
  PipelineEditorProps
>(function PipelineEditor(
  { pipeline, updateWorkflow, onBack, onExport, inSidebar },
  ref,
) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog();

  const initDef = useMemo(() => {
    try {
      return JSON.parse(pipeline.definition_json) as Record<string, unknown>;
    } catch {
      return {} as Record<string, unknown>;
    }
  }, [pipeline.definition_json]);

  const initSteps = useMemo(
    () =>
      Array.isArray(initDef.steps) ? (initDef.steps as PipelineStep[]) : [],
    [initDef],
  );

  const [name, setName] = useState(pipeline.name);
  const [description, setDescription] = useState(pipeline.description ?? "");
  const [steps, setSteps] = useState<PipelineStep[]>(initSteps);
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [isDirty, setDirty] = useState(false);
  const [loadedPipelineId, setLoadedPipelineId] = useState(pipeline.id);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Reset editor state when a different pipeline is loaded into this instance.
  // Adjusting state during render matches existing codebase convention and
  // avoids an extra commit/flash.
  if (loadedPipelineId !== pipeline.id) {
    setLoadedPipelineId(pipeline.id);
    setName(pipeline.name);
    setDescription(pipeline.description ?? "");
    setSteps(initSteps);
    setExpandedIndex(null);
    setDirty(false);
    setSaveError(null);
  }

  const markDirty = useCallback(() => setDirty(true), []);

  const handleBack = useCallback(async () => {
    if (
      isDirty &&
      !(await confirm({
        title: "Unsaved changes",
        description: "You have unsaved changes. Discard them?",
        confirmLabel: "Discard",
        destructive: true,
      }))
    ) {
      return;
    }
    onBack();
  }, [isDirty, onBack, confirm]);

  const updateStep = useCallback(
    (index: number, updates: Partial<PipelineStep>) => {
      setSteps((prev) =>
        prev.map((step, i) => (i === index ? { ...step, ...updates } : step)),
      );
      markDirty();
    },
    [markDirty],
  );

  const deleteStep = useCallback(
    async (index: number) => {
      if (
        !(await confirm({
          title: "Delete step?",
          confirmLabel: "Delete",
          destructive: true,
        }))
      ) {
        return;
      }
      setSteps((prev) => prev.filter((_, i) => i !== index));
      setExpandedIndex(null);
      markDirty();
    },
    [markDirty, confirm],
  );

  const moveStep = useCallback(
    (index: number, direction: -1 | 1) => {
      setSteps((prev) => {
        const next = [...prev];
        const target = index + direction;
        if (target < 0 || target >= next.length) return prev;
        [next[index], next[target]] = [next[target], next[index]];
        return next;
      });
      markDirty();
    },
    [markDirty],
  );

  const addStep = useCallback(
    (type: StepType) => {
      const ids = steps.map((step) => step.id);
      const step = createDefaultStep(type, ids);
      setSteps((prev) => [...prev, step]);
      setExpandedIndex(steps.length);
      markDirty();
    },
    [steps, markDirty],
  );

  const changeStepType = useCallback(
    (index: number, newType: StepType) => {
      setSteps((prev) =>
        prev.map((step, i) =>
          i === index ? changeStepPayload(step, newType) : step,
        ),
      );
      markDirty();
    },
    [markDirty],
  );

  const handleSave = useCallback(async () => {
    const ids = steps.map((step) => step.id);
    const dupes = ids.filter((id, i) => ids.indexOf(id) !== i);
    if (dupes.length > 0) {
      setSaveError(`Duplicate step IDs: ${dupes.join(", ")}`);
      return;
    }

    setSaveError(null);
    setSaving(true);
    try {
      const def: Record<string, unknown> = { ...initDef };
      def.name = name.trim() || pipeline.name;
      def.description = description.trim() || undefined;
      def.steps = steps;
      const saved = await updateWorkflow(pipeline.id, {
        name: name.trim() || pipeline.name,
        description: description.trim() || undefined,
        definition_json: JSON.stringify(def),
      });
      if (!saved) {
        setSaveError("Could not save the pipeline. Please try again.");
        return;
      }
      setDirty(false);
    } catch (e) {
      setSaveError(
        `Save failed: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setSaving(false);
    }
  }, [steps, name, description, initDef, pipeline, updateWorkflow]);

  useImperativeHandle(
    ref,
    () => ({
      save: handleSave,
      isDirty,
    }),
    [handleSave, isDirty],
  );

  return (
    <div
      className={cn(
        "flex h-full flex-1 flex-col overflow-hidden",
        inSidebar && "!h-auto !overflow-visible",
      )}
    >
      {ConfirmDialogElement}
      {!inSidebar && (
        <div className="flex shrink-0 items-center justify-between gap-4 border-b border-border bg-[var(--bg-secondary)] px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <Button
              type="button"
              size="sm"
              className={cn(coarseHitAreaCls, "text-base")}
              onClick={handleBack}
            >
              &larr;
            </Button>
            <Input
              type="text"
              wrapperClassName="w-[240px]"
              className="h-8 border-transparent bg-transparent px-2.5 py-1 text-base font-semibold hover:bg-[var(--bg-tertiary)] focus-visible:bg-[var(--bg-primary)]"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                markDirty();
              }}
              placeholder="Pipeline name"
            />
            <Chip tone="accent" uppercase>
              pipeline
            </Chip>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              className={coarseHitAreaCls}
              onClick={onExport}
            >
              Export YAML
            </Button>
            <Button
              type="button"
              variant="primary"
              size="sm"
              className={coarseHitAreaCls}
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? "Saving..." : "Save"}
            </Button>
          </div>
        </div>
      )}

      {saveError && (
        <div
          className="mx-3 mb-1 rounded-md bg-[color-mix(in_srgb,var(--color-error)_12%,transparent)] px-3 py-2 text-sm text-[var(--color-error)]"
          role="alert"
        >
          {saveError}
        </div>
      )}

      <FormField
        label="Description"
        className="shrink-0 gap-1 border-b border-border px-4 py-3 [&>label:first-child]:text-xs [&>label:first-child]:font-semibold [&>label:first-child]:tracking-[0.5px] [&>label:first-child]:uppercase"
      >
        {({ id, describedBy, invalid }) => (
          <Textarea
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            className="min-h-10 resize-y px-2.5 py-2 font-[inherit] text-md"
            value={description}
            onChange={(e) => {
              setDescription(e.target.value);
              markDirty();
            }}
            placeholder="Pipeline description..."
            rows={2}
          />
        )}
      </FormField>

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
  );
});
