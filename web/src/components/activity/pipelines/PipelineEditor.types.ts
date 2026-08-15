import type { PipelineDefDetail } from "../../../hooks/usePipelineDefs";

export type StepType = "exec" | "prompt" | "mcp" | "invoke_pipeline";

export interface PipelineStep {
  id: string;
  [key: string]: unknown;
}

export interface KVPair {
  key: string;
  value: string;
}

export interface PipelineEditorHandle {
  save: () => Promise<void>;
  isDirty: boolean;
}

export interface PipelineEditorProps {
  pipeline: PipelineDefDetail;
  updateWorkflow: (
    id: string,
    params: { name?: string; definition_json?: string; description?: string },
  ) => Promise<PipelineDefDetail | null>;
  onBack: () => void;
  onExport: () => void;
  inSidebar?: boolean;
}

export type StepChangeHandler = (updates: Partial<PipelineStep>) => void;
