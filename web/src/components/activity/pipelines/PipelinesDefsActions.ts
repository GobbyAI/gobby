import type { WorkflowDetail } from "../../../hooks/useWorkflows";

export type PipelineDefinition = WorkflowDetail;

export interface PipelineDefinitionUpdate {
  name?: string;
  definition_json?: string;
  description?: string;
  enabled?: boolean;
  tags?: string[];
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

async function parseDefinitionResponse(
  response: Response,
): Promise<PipelineDefinition | null> {
  if (!response.ok) return null;
  const data = await response.json();
  return data.definition ?? null;
}

export async function loadPipelineDefinitions(
  projectId?: string | null,
): Promise<PipelineDefinition[]> {
  const params = new URLSearchParams({
    workflow_type: "pipeline",
    include_deleted: "true",
  });
  if (projectId) params.set("project_id", projectId);

  const response = await fetch(`${getBaseUrl()}/api/workflows?${params}`);
  if (!response.ok) {
    throw new Error(`Failed to load pipeline definitions (${response.status})`);
  }
  const data = await response.json();
  return data.definitions ?? [];
}

export async function updatePipelineDefinition(
  id: string,
  updates: PipelineDefinitionUpdate,
): Promise<PipelineDefinition | null> {
  const response = await fetch(
    `${getBaseUrl()}/api/workflows/${encodeURIComponent(id)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    },
  );
  return parseDefinitionResponse(response);
}

export async function togglePipelineDefinition(
  id: string,
): Promise<PipelineDefinition | null> {
  const response = await fetch(
    `${getBaseUrl()}/api/workflows/${encodeURIComponent(id)}/toggle`,
    {
      method: "PUT",
    },
  );
  return parseDefinitionResponse(response);
}

export async function deletePipelineDefinition(id: string): Promise<boolean> {
  const response = await fetch(
    `${getBaseUrl()}/api/workflows/${encodeURIComponent(id)}`,
    {
      method: "DELETE",
    },
  );
  if (!response.ok) return false;
  const data = await response.json();
  return Boolean(data.deleted);
}

export async function exportPipelineYaml(id: string): Promise<string | null> {
  const response = await fetch(
    `${getBaseUrl()}/api/workflows/${encodeURIComponent(id)}/export`,
  );
  return response.ok ? response.text() : null;
}

export async function runPipelineDefinition(
  name: string,
  projectId?: string | null,
): Promise<boolean> {
  const response = await fetch(`${getBaseUrl()}/api/pipelines/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, inputs: {}, project_id: projectId ?? "" }),
  });
  return response.ok || response.status === 202;
}
