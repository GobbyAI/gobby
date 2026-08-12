import type { PipelineStep, StepType } from "./PipelineEditor.types";

export const STEP_TYPES: { value: StepType; label: string; color: string }[] = [
  { value: "exec", label: "Exec", color: "var(--step-type-exec)" },
  { value: "prompt", label: "Prompt", color: "var(--step-type-prompt)" },
  { value: "mcp", label: "MCP", color: "var(--step-type-mcp)" },
  {
    value: "invoke_pipeline",
    label: "Pipeline",
    color: "var(--step-type-invoke_pipeline)",
  },
];

const STEP_TYPE_VALUES = STEP_TYPES.map((type) => type.value);

export function stripTemplateWrapper(value: string): string {
  const match = value.match(/^\$\{\{\s*(.*?)\s*\}\}$/);
  return match ? match[1].trim() : value;
}

export function wrapTemplateExpr(value: string): string {
  return `\${{ ${value} }}`;
}

export function detectStepType(step: PipelineStep): StepType {
  if (step.exec != null) return "exec";
  if (step.prompt != null) return "prompt";
  if (step.mcp != null) return "mcp";
  if (step.invoke_pipeline != null) return "invoke_pipeline";
  return "exec";
}

export function getTypeColor(type: StepType): string {
  return (
    STEP_TYPES.find((item) => item.value === type)?.color ?? "var(--text-muted)"
  );
}

export function getStepPreview(step: PipelineStep): string {
  const type = detectStepType(step);
  let preview = "";
  if (type === "exec") {
    preview = (step.exec as string) ?? "";
  } else if (type === "prompt") {
    preview = (step.prompt as string) ?? "";
  } else if (type === "mcp") {
    const mcp = step.mcp as Record<string, unknown> | undefined;
    preview = mcp ? `${mcp.server ?? ""}/${mcp.tool ?? ""}` : "";
  } else if (type === "invoke_pipeline") {
    const invokePipeline = step.invoke_pipeline;
    preview =
      typeof invokePipeline === "string"
        ? invokePipeline
        : (((invokePipeline as Record<string, unknown>)?.name as string) ?? "");
  }
  return preview.length > 60 ? `${preview.slice(0, 57)}...` : preview;
}

export function createDefaultStep(
  type: StepType,
  existingIds: string[],
): PipelineStep {
  const base = "step";
  let n = existingIds.length + 1;
  while (existingIds.includes(`${base}-${n}`)) n++;
  return changeStepPayload({ id: `${base}-${n}` }, type);
}

export function changeStepPayload(
  step: PipelineStep,
  type: StepType,
): PipelineStep {
  const cleaned = { ...step };
  for (const stepType of STEP_TYPE_VALUES) {
    delete cleaned[stepType];
  }

  if (type === "exec") cleaned.exec = "";
  else if (type === "prompt") cleaned.prompt = "";
  else if (type === "mcp")
    cleaned.mcp = { server: "", tool: "", arguments: {} };
  else if (type === "invoke_pipeline") cleaned.invoke_pipeline = "";
  return cleaned;
}
