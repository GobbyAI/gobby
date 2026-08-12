import { useEffect, useRef, useState } from "react";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Card } from "../../ui/Card";
import { Chip } from "../../ui/Chip";
import { FormField } from "../../ui/FormField";
import { Input } from "../../ui/Input";
import { NativeSelect } from "../../ui/NativeSelect";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import type {
  PipelineStep,
  StepChangeHandler,
  StepType,
} from "./PipelineEditor.types";
import {
  STEP_TYPES,
  detectStepType,
  getStepPreview,
  getTypeColor,
} from "./pipelineStepModel";
import {
  CommonFields,
  ExecFields,
  InvokePipelineFields,
  McpFields,
  PromptFields,
} from "./PipelineStepFields";

type PipelineStepListProps = {
  steps: PipelineStep[];
  expandedIndex: number | null;
  inSidebar?: boolean;
  onExpandedIndexChange: (index: number | null) => void;
  onUpdateStep: (index: number, updates: Partial<PipelineStep>) => void;
  onDeleteStep: (index: number) => void | Promise<void>;
  onMoveStep: (index: number, direction: -1 | 1) => void;
  onChangeStepType: (index: number, type: StepType) => void;
  onAddStep: (type: StepType) => void;
};

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
    <div
      className={cn(
        "flex-1 overflow-y-auto px-4 pt-3 pb-5",
        inSidebar && "!overflow-visible !pb-0",
      )}
    >
      <div className="text-secondary mb-2.5 flex items-center gap-2 text-xs font-semibold tracking-[0.5px] uppercase">
        Steps
        <Chip>{steps.length}</Chip>
      </div>

      {steps.length === 0 && (
        <div className="text-secondary p-6 text-center text-md">
          No steps yet. Add one below.
        </div>
      )}

      {steps.map((step, index) => (
        <PipelineStepCard
          key={index}
          step={step}
          index={index}
          totalSteps={steps.length}
          expanded={expandedIndex === index}
          onToggle={() =>
            onExpandedIndexChange(expandedIndex === index ? null : index)
          }
          onUpdate={(updates) => onUpdateStep(index, updates)}
          onDelete={() => void onDeleteStep(index)}
          onMove={(direction) => onMoveStep(index, direction)}
          onChangeType={(type) => onChangeStepType(index, type)}
        />
      ))}

      <AddStepButton onAdd={onAddStep} />
    </div>
  );
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
  step: PipelineStep;
  index: number;
  totalSteps: number;
  expanded: boolean;
  onToggle: () => void;
  onUpdate: StepChangeHandler;
  onDelete: () => void;
  onMove: (direction: -1 | 1) => void;
  onChangeType: (type: StepType) => void;
}) {
  const type = detectStepType(step);
  const typeColor = getTypeColor(type);

  return (
    <Card className="mb-2 overflow-hidden bg-[var(--bg-secondary)]">
      <Button
        type="button"
        variant="ghost"
        className={cn(
          coarseHitAreaCls,
          "h-auto w-full justify-start rounded-none border-0 px-3 py-2.5 text-left hover:bg-[var(--bg-tertiary)]",
        )}
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <Chip
          style={{
            background: `color-mix(in srgb, ${typeColor} 12%, transparent)`,
            color: typeColor,
          }}
        >
          {type}
        </Chip>
        <span className="text-primary shrink-0 text-md font-medium">
          {step.id}
        </span>
        <span className="text-secondary min-w-0 flex-1 overflow-hidden text-sm text-ellipsis whitespace-nowrap">
          {getStepPreview(step)}
        </span>
        <span className="text-secondary ml-auto shrink-0 text-sm">
          {expanded ? "▾" : "▸"}
        </span>
      </Button>

      {expanded && (
        <div className="border-t border-border px-3 pb-3">
          <div className="flex gap-1.5 py-2">
            <Button
              type="button"
              size="sm"
              className={coarseHitAreaCls}
              onClick={() => onMove(-1)}
              disabled={index === 0}
              title="Move up"
            >
              &uarr;
            </Button>
            <Button
              type="button"
              size="sm"
              className={coarseHitAreaCls}
              onClick={() => onMove(1)}
              disabled={index === totalSteps - 1}
              title="Move down"
            >
              &darr;
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              className={coarseHitAreaCls}
              onClick={onDelete}
              title="Delete step"
            >
              Delete
            </Button>
          </div>

          <FormField
            label="Step ID"
            className="mb-2.5 [&>label:first-child]:text-xs"
          >
            {({ id, describedBy, invalid }) => (
              <Input
                id={id}
                aria-describedby={describedBy}
                error={invalid}
                type="text"
                value={step.id}
                onChange={(e) => onUpdate({ id: e.target.value })}
              />
            )}
          </FormField>

          <FormField
            label="Type"
            className="mb-2.5 [&>label:first-child]:text-xs"
          >
            {({ id, describedBy, invalid }) => (
              <NativeSelect
                id={id}
                aria-describedby={describedBy}
                error={invalid}
                value={type}
                onChange={(e) => onChangeType(e.target.value as StepType)}
              >
                {STEP_TYPES.map((stepType) => (
                  <option key={stepType.value} value={stepType.value}>
                    {stepType.label}
                  </option>
                ))}
              </NativeSelect>
            )}
          </FormField>

          {type === "exec" && <ExecFields step={step} onChange={onUpdate} />}
          {type === "prompt" && (
            <PromptFields step={step} onChange={onUpdate} />
          )}
          {type === "mcp" && <McpFields step={step} onChange={onUpdate} />}
          {type === "invoke_pipeline" && (
            <InvokePipelineFields step={step} onChange={onUpdate} />
          )}
          <CommonFields step={step} type={type} onChange={onUpdate} />
        </div>
      )}
    </Card>
  );
}

function AddStepButton({ onAdd }: { onAdd: (type: StepType) => void }) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="relative mt-2" ref={containerRef}>
      <Button
        type="button"
        variant="outline"
        className={cn(coarseHitAreaCls, "h-auto w-full border-dashed p-2.5")}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        + Add Step
      </Button>
      {open && (
        <Card className="absolute bottom-full left-0 z-10 mb-1 min-w-40 bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]">
          {STEP_TYPES.map((stepType) => (
            <Button
              key={stepType.value}
              type="button"
              variant="ghost"
              className={cn(
                coarseHitAreaCls,
                "h-auto w-full justify-start px-2.5 py-2 text-md",
              )}
              onClick={() => {
                onAdd(stepType.value);
                setOpen(false);
              }}
            >
              <span
                className="size-2 shrink-0 rounded-full"
                style={{ background: stepType.color }}
              />
              {stepType.label}
            </Button>
          ))}
        </Card>
      )}
    </div>
  );
}
